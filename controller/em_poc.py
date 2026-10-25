"""Mercury POC second sink — EchoMuse FORK addition (not upstream).

Routes post-wake Dot audio to the Mercury POC (POST /api/turn) instead of
Home Assistant's voice pipeline, per the device's voiceRoute config:

  ha            stock HA turn (default; upstream behaviour, unchanged)
  echomuse_mercury  EchoMuse Mercury first; on POC failure, replay the
                buffered utterance into a stock HA turn (staging mode)
  poc_only      POC first; on POC failure the turn ends quietly (cutover
                mode — HA voice is deliberately NOT a fallback here)
  shadow        stock HA turn, plus a tap of the mic audio is transcribed
                and Decide-routed by the POC for logging only: shadow
                never acts, never speaks, never touches turn history

Why this module exists: the Dot never picks a voice endpoint — it talks
only to this controller, and HA drives the turn through the ESPHome
satellite. The fork therefore routes at the conversation-management layer,
inside _run_voice_locked's turn loop, after wake + Silero gating and before
the HA voice session opens. The ESPHome server stays up in every mode, so
the media-player / timer / announcement entities served off the same
satellite connection keep working — voice diversion never touches them.
Media separability is a gating acceptance test for cutover, not an
assumption: it is proven live, not by this comment.

Turn-end semantics (the acceptance bar: no hangs, no cut-offs, no double
answers):
  - Capture ends on: device VAD-end sentinel, device no-speech sentinel,
    controller no-speech verdict (em_turnclock), a ~1 s post-speech
    silence backstop, the 30 s max-open cap, or cancel.
  - The POC call is synchronous (utterance-at-once v1): one POST per turn.
  - Fallback replay is allowed ONLY when the POC demonstrably never ran:
    connection refused/unreachable, or an HTTP 400 validation rejection
    (the POC rejects malformed requests before acting). Timeouts, 5xx,
    and malformed replies are ambiguous — the POC may already have
    executed a device action — so they NEVER replay: replaying there
    could double-execute an action. The turn ends quietly instead.
  - Barge-in during POC playback goes through the turn loop's existing
    cancel path (the same on_thinking/post_turn_play closures as the HA
    path), so the device cuts its own reply and no second answer starts.
  - Capture deliberately skips the NS DTLN denoiser the HA path applies:
    the POC's faster-whisper receives raw mic audio, the same input the
    browser POC uses. Denoised capture is a v2 improvement, not a v1 gap
    in endpointing.

Deliberate import discipline: this module imports only stdlib plus
em_turnclock (which itself imports nothing) and em_tasks (asyncio +
logging only) at top level. aiohttp, em_speechgate, em_events and
em_esphome are imported lazily inside the functions that need them, so
the capture/router logic stays unit-testable without the controller's
heavy dependencies — and so a missing optional dependency degrades (RMS
gating, no DB persistence) instead of breaking the turn loop.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import struct
import time
import uuid
import wave

import em_tasks
import em_turnclock

log = logging.getLogger("echomuse.poc")

# ─── Routing ────────────────────────────────────────────────────────────────

ROUTES = ("ha", "echomuse_mercury", "poc_only", "shadow")

DEFAULT_POC_ENDPOINT = "http://127.0.0.1:8896"

# Utterance-at-once v1: the POC call includes LLM time, so the budget is
# the whole turn, not HA's 30 s first-audio wait. Streaming v2 restores
# first-audio semantics.
POC_TURN_TIMEOUT_S = 60.0
SHADOW_TIMEOUT_S = 30.0

# Seconds of post-speech silence before the controller endpoints the turn
# itself. Matches em_turnclock.FALLBACK_SILENCE (1.0 s) — the device's own
# vadSilenceMs default is 900 ms, so this does not cut sooner than the
# device-gated modes already do. Tunable via EM_POC_SILENCE_END_S.
SILENCE_END_S = 1.0

# Device max-open cap: a session the device never closes ends here, and
# the buffered audio is still sent to the POC (better an answer to a long
# command than a dropped one).
MAX_TURN_S = 30.0

# POC reply framing: 48 kHz mono 16-bit, chunked into 100 ms pieces for
# the speaker path (9600 bytes = 0.1 s at 48000 Hz mono s16).
REPLY_CHUNK_BYTES = 9600

# Shadow tap cap: ~60 s of 16 kHz mono s16 audio. Longer turns stop
# tapping rather than growing the buffer unboundedly.
SHADOW_TAP_CAP = 2_000_000

# Replay pacing: voice_queue frames are ~80 ms (2560 bytes at 16 kHz s16).
REPLAY_FRAME_BYTES = 2560


def resolve_route(device) -> str:
    """This device's voice route, fail-safe to stock HA behaviour."""
    raw = getattr(device, "voice_route", None)
    if raw is None or str(raw).strip() == "":
        raw = os.environ.get("EM_VOICE_ROUTE", "")
    route = str(raw).strip()
    if route not in ROUTES:
        if route:
            log.warning(f"[poc] unknown voice_route {route!r} — falling back to 'ha'")
        return "ha"
    return route


def poc_endpoint(device) -> str:
    """The POC base URL for this device, fail-safe to the thinclient default."""
    raw = getattr(device, "poc_endpoint", None)
    if raw is None or str(raw).strip() == "":
        raw = os.environ.get("EM_POC_ENDPOINT", "")
    url = str(raw).strip().rstrip("/")
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url:
        log.warning(f"[poc] ignoring non-http poc_endpoint {url!r}")
    return DEFAULT_POC_ENDPOINT


def silence_end_s() -> float:
    try:
        return max(0.2, float(os.environ.get("EM_POC_SILENCE_END_S", SILENCE_END_S)))
    except (TypeError, ValueError):
        return SILENCE_END_S


# ─── Session media pause (fork wake-wiring) ───────────────────────────────

# The POC pauses HA media at voice-session start (only if the player is
# actually playing, so a later resume never restarts user-paused media)
# and resumes it at session end (only if the POC paused it); a 90 s
# auto-resume covers a session_end that never arrives. The fork therefore
# POSTs /api/session_start at turn start and /api/session_end when a POC
# turn ends before the POC ever ran (no-speech / cancelled / empty —
# turns the POC did run own their resume via /api/turn, and stop turns
# must stay stopped, so neither posts a session_end).
#
# These calls must NEVER break, delay, or fail a voice turn: short
# timeout, every failure swallowed to a log line, and both spawns are
# fire-and-forget via em_tasks.spawn — the turn loop never awaits them.
# They reuse poc_endpoint() exactly like post_turn: no new URL is
# hardcoded anywhere. Gated to the POC routes: HA and shadow turns keep
# their stock media behaviour untouched.
SESSION_POST_TIMEOUT_S = 2.0

POC_SESSION_ROUTES = ("echomuse_mercury", "poc_only")


def session_id_for(device) -> str:
    """The POC session id for this turn: the private session, else fresh."""
    sid = getattr(device, "listen_session", None)
    return str(sid) if sid is not None else str(uuid.uuid4())


async def post_session(endpoint: str, path: str, session_id: str,
                       device_id: str,
                       timeout_s: float = SESSION_POST_TIMEOUT_S) -> dict:
    """POST one session lifecycle call. Raises on any failure.

    Callers swallow: a failed session post is a log line, never a turn
    failure. Returns the POC's JSON object (booleans only are logged).
    """
    try:
        import aiohttp
    except ImportError as e:
        raise PocError(f"aiohttp unavailable for POC session call: {e}")
    payload = {"session_id": session_id, "device": device_id}
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(endpoint + path, json=payload) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise PocError(f"POC {path} HTTP {resp.status}")
                try:
                    data = await resp.json()
                except Exception as e:
                    raise PocError(f"POC {path} reply is not JSON: {e}")
    except PocError:
        raise
    except asyncio.TimeoutError as e:
        raise PocError(f"POC {path} timed out after {timeout_s}s: {e}")
    except Exception as e:
        raise PocError(f"POC {path} failed: {e}")
    if not isinstance(data, dict):
        raise PocError(f"POC {path} reply is not a JSON object")
    return data


def _spawn_session(device, path: str) -> None:
    device_id = getattr(device, "device_id", "unknown")
    if resolve_route(device) not in POC_SESSION_ROUTES:
        return
    endpoint = poc_endpoint(device)
    session_id = session_id_for(device)

    async def _post() -> None:
        try:
            result = await post_session(endpoint, path, session_id, device_id)
            if path == "/api/session_start":
                log.info(f"[poc] session_start {device_id}: "
                         f"paused={result.get('paused')} "
                         f"state_before={result.get('player_state_before')}")
            else:
                log.info(f"[poc] session_end {device_id}: "
                         f"resumed={result.get('resumed')}")
        except Exception as e:
            log.warning(f"[poc] {path} {device_id} failed "
                        f"(best-effort, turn unaffected): {e}")
    try:
        em_tasks.spawn(_post())
    except RuntimeError as e:
        log.warning(f"[poc] {path} not scheduled (no loop): {e}")


def spawn_session_start(device) -> None:
    """Fire-and-forget POC /api/session_start for a POC-route turn."""
    _spawn_session(device, "/api/session_start")


def spawn_session_end(device) -> None:
    """Fire-and-forget POC /api/session_end for a POC-route turn."""
    _spawn_session(device, "/api/session_end")


# ─── POC client ─────────────────────────────────────────────────────────────

class PocError(Exception):
    """POC call failed in a way that must NOT fall back (ambiguous)."""


class PocConnectionError(PocError):
    """POC unreachable: it demonstrably never ran, so fallback is safe."""


class PocRejected(PocError):
    """POC rejected the request (HTTP 400) before acting: fallback safe."""


class PocTimeout(PocError):
    """POC call timed out: it may already have acted, so NO fallback."""


def failure_replayable(exc: Exception) -> bool:
    """True only when the POC demonstrably never ran.

    This is the no-double-answer rule in one predicate: connection errors
    and validation rejections happen before any POC action; everything
    else (timeout, 5xx, malformed reply) is ambiguous and must not replay.
    """
    return isinstance(exc, (PocConnectionError, PocRejected))


class PocResult:
    def __init__(self, transcript, decide_verdict, decide_confidence,
                 actions, engine, stop, audio_pcm):
        self.transcript = transcript
        self.decide_verdict = decide_verdict
        self.decide_confidence = decide_confidence
        self.actions = actions
        self.engine = engine
        self.stop = stop
        self.audio_pcm = audio_pcm


def build_wav(pcm: bytes, rate: int = 16000) -> bytes:
    """Mono s16 PCM -> WAV bytes (the POC's /api/turn audio format)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def parse_wav48k(data: bytes) -> bytes:
    """Validate a POC reply (48 kHz mono WAV) and return its raw PCM."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2 \
                    or w.getframerate() != 48000:
                raise PocError(
                    f"POC reply is not 48kHz mono s16: "
                    f"{w.getnchannels()}ch/{w.getsampwidth()}B/{w.getframerate()}Hz")
            return w.readframes(w.getnframes())
    except PocError:
        raise
    except Exception as e:
        raise PocError(f"POC reply is not a decodable WAV: {e}")


async def post_turn(endpoint: str, pcm: bytes, session_id: str,
                    device_id: str, barge: bool, end_reason: str,
                    mode: str = "live",
                    timeout_s: float = POC_TURN_TIMEOUT_S) -> PocResult:
    """One synchronous POC turn. Raises PocError subclasses on failure."""
    try:
        import aiohttp
    except ImportError as e:
        raise PocError(f"aiohttp unavailable for POC call: {e}")
    payload = {
        "session_id": session_id,
        "device": device_id,
        "barge": barge,
        "audio_b64": base64.b64encode(build_wav(pcm)).decode("ascii"),
        "end_reason": end_reason,
        "mode": mode,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(endpoint + "/api/turn", json=payload) as resp:
                if resp.status == 400:
                    raise PocRejected(f"POC rejected turn: HTTP 400")
                if resp.status < 200 or resp.status >= 300:
                    raise PocError(f"POC turn HTTP {resp.status}")
                try:
                    data = await resp.json()
                except Exception as e:
                    # A 200 with a non-JSON body is a broken POC, not a
                    # rejection: ambiguous, so it must not fall back.
                    raise PocError(f"POC turn reply is not JSON: {e}")
    except PocError:
        raise
    except asyncio.TimeoutError as e:
        raise PocTimeout(f"POC turn timed out after {timeout_s}s: {e}")
    except (aiohttp.ClientConnectorError, aiohttp.ClientOSError) as e:
        raise PocConnectionError(f"POC unreachable at {endpoint}: {e}")
    except aiohttp.ClientError as e:
        raise PocError(f"POC turn failed: {e}")
    if not isinstance(data, dict):
        raise PocError("POC turn reply is not a JSON object")
    audio_b64 = data.get("audio_b64") or ""
    if data.get("stop") or not audio_b64:
        audio_pcm = b""
    else:
        try:
            audio_pcm = parse_wav48k(base64.b64decode(audio_b64))
        except Exception as e:
            raise PocError(f"POC reply audio unusable: {e}")
    return PocResult(
        transcript=data.get("transcript") or "",
        decide_verdict=data.get("decide_verdict"),
        decide_confidence=data.get("decide_confidence"),
        actions=data.get("actions") or [],
        engine=data.get("engine"),
        stop=bool(data.get("stop")),
        audio_pcm=audio_pcm,
    )


# ─── Capture ────────────────────────────────────────────────────────────────

def rms_speech(chunk: bytes, floor: float) -> bool:
    """The HA path's speech bar, without numpy: RMS >= max(2x floor, 0.004).

    Bar lowered 3x -> 2x floor (Oct 24 2026 ASR improvement): at the
    observed floor 0.0275 the old bar was 0.0825 RMS and clipped quiet
    word onsets ("time is it", "the lamp off"); the new bar is 0.055.
    """
    if len(chunk) < 2:
        return False
    n = len(chunk) // 2
    samples = struct.unpack(f"<{n}h", chunk[:n * 2])
    mean_sq = sum((s / 32768.0) ** 2 for s in samples) / n
    return mean_sq ** 0.5 >= max(2.0 * floor, 0.004)


async def capture_turn(device, preroll_discard: int,
                       silence_end: float | None = None,
                       max_turn: float = MAX_TURN_S) -> tuple[bytes, str]:
    """Buffer one utterance from device.voice_queue until it endpoints.

    Returns (pcm_bytes, end_reason) with end_reason in vad_end, no_speech,
    cancelled, max_open. Held (pre-speech) gate frames are discarded, the
    same as the HA path: unreleased audio is pre-speech noise by
    definition. The caller decides what an empty buffer means; this
    function never calls the POC itself.
    """
    if silence_end is None:
        silence_end = silence_end_s()
    try:
        import em_speechgate
        vad = em_speechgate.new_turn()
        gate = em_speechgate.SpeechGate() if vad is not None else None
    except Exception:
        vad = None
        gate = None
    loop = asyncio.get_running_loop()
    queue = device.voice_queue
    cancel = device.cancel_event
    floor = getattr(device, "noise_floor", 0.0)
    buf = bytearray()
    turn_start = loop.time()
    listening_since = None
    speech_seen = False
    last_speech_at = None
    preroll = max(0, preroll_discard)
    while True:
        now = loop.time()
        if cancel.is_set():
            return bytes(buf), "cancelled"
        if speech_seen and last_speech_at is not None \
                and now - last_speech_at > silence_end:
            return bytes(buf), "vad_end"
        close, _why = em_turnclock.no_speech_verdict(
            now, turn_start, listening_since, speech_seen)
        if close:
            return bytes(buf), "no_speech"
        if now - turn_start > max_turn:
            return bytes(buf), "max_open"
        try:
            item = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        if item is None or isinstance(item, str):
            # Device sentinel: end the turn. Unknown sentinels end it too
            # — a sentinel the controller does not recognise is still the
            # device saying the turn is over, never a reason to hang.
            if item == "vad_no_speech_timeout":
                return bytes(buf), "no_speech"
            return bytes(buf), "vad_end"
        if preroll > 0:
            preroll -= 1
            continue
        if listening_since is None:
            listening_since = loop.time()
        released = [item]
        if gate is not None and vad is not None:
            try:
                prob = await loop.run_in_executor(None, vad.prob, item)
                released = gate.push(item, prob)
            except Exception:
                released = [item]
        for frame in released:
            buf.extend(frame)
            if rms_speech(frame, floor):
                speech_seen = True
                last_speech_at = loop.time()
        if gate is not None and gate.open:
            speech_seen = True


# ─── Turn recording ─────────────────────────────────────────────────────────

async def _record_turn(device, record: dict) -> None:
    """Turn history + live event + best-effort DB row, same shape as HA turns.

    Extra keys (backend, decide_*) are ignored by db.insert_turn, which
    reads only known columns — so a POC turn row is a normal turn row with
    the POC fields visible in memory and on the dashboard event stream.
    """
    history = getattr(device, "turn_history", None)
    if isinstance(history, list):
        history.append(record)
    try:
        import em_events
        await em_events.push({
            "type": "turn_complete",
            "device_id": getattr(device, "device_id", "unknown"),
            "turn": record,
        })
    except Exception:
        pass
    try:
        import em_esphome
        await em_esphome._persist_turn(device, record)
    except Exception as e:
        log.warning(f"[poc] turn DB persist skipped: {e}")


# ─── Shadow ─────────────────────────────────────────────────────────────────

def capped_tap(buf: bytearray, cap: int = SHADOW_TAP_CAP):
    """A _stream_mic_audio tap that stops growing past `cap` bytes."""
    def tap(frame: bytes) -> None:
        room = cap - len(buf)
        if room > 0:
            buf.extend(frame[:room])
    return tap


def spawn_shadow_post(device, pcm: bytes, trigger_label: str) -> None:
    """Fire-and-forget shadow POC call: transcribe + Decide, log only.

    Never awaits the turn loop, never acts, never speaks, never records
    a turn. A shadow failure is a log line, never a user-visible event.
    """
    async def _shadow() -> None:
        device_id = getattr(device, "device_id", "unknown")
        try:
            result = await post_turn(
                poc_endpoint(device), pcm, str(uuid.uuid4()), device_id,
                trigger_label == "barge-in", "vad_end",
                mode="shadow", timeout_s=SHADOW_TIMEOUT_S)
            log.info(f"[poc] shadow {device_id}: transcript="
                     f"{result.transcript!r} decide={result.decide_verdict} "
                     f"conf={result.decide_confidence}")
        except Exception as e:
            log.warning(f"[poc] shadow {device_id} failed (log-only): {e}")
    try:
        # Held by em_tasks.spawn, not discarded: a bare create_task is a
        # leak the task-suite guard (test_tasks.py) rightly refuses.
        em_tasks.spawn(_shadow())
    except RuntimeError as e:
        log.warning(f"[poc] shadow not scheduled (no loop): {e}")


# ─── POC turn runner ────────────────────────────────────────────────────────

async def _chunks(pcm: bytes):
    for i in range(0, len(pcm), REPLY_CHUNK_BYTES):
        yield pcm[i:i + REPLY_CHUNK_BYTES]


async def _replay_and_fallback(device, pcm: bytes, put_frame, sentinel_end,
                               ha_turn) -> bool:
    """Paced replay of the buffered utterance into a stock HA turn.

    Frames are queue.put (blocking when full), never put_nowait: the
    queue holds 256 frames (~20 s), and dropping the oldest on overflow
    would cut the START of the command. The replay task is cancelled as
    soon as the HA turn returns, so a stalled consumer cannot hang the
    loop; ordinary utterances (<20 s) replay whole before HA's own
    20 s streaming cap.
    """
    async def _replay() -> None:
        for i in range(0, len(pcm), REPLAY_FRAME_BYTES):
            await put_frame(pcm[i:i + REPLAY_FRAME_BYTES])
        await put_frame(sentinel_end)
    replay = asyncio.create_task(_replay())
    try:
        return await ha_turn()
    finally:
        if not replay.done():
            replay.cancel()


async def run_poc_turn(device, *, on_thinking, post_turn_play,
                       trigger_label: str, preroll_discard: int,
                       ha_turn, put_frame, sentinel_end: str,
                       allow_fallback: bool) -> bool:
    """Run one POC turn. Returns should_continue (always False in v1).

    Callbacks (all supplied by _run_voice_locked, so this module never
    imports em_controller):
      on_thinking    — the loop's thinking transition (session close for
                       private listening, spinner + barge watcher else)
      post_turn_play — the loop's speaker path (LED/meter/barge handling)
      ha_turn        — stock trigger_voice_turn with preroll_discard=0
      put_frame      — awaitable queue put for fallback replay
    """
    # Pop (not read) the wake detail, the same as trigger_voice_turn does
    # for HA turns, so a continuation turn never inherits this wake's score.
    wake_info = device.last_wake
    device.last_wake = None
    wake_score = wake_info.get("score") if isinstance(wake_info, dict) else None
    t0 = time.monotonic()
    pcm, end_reason = await capture_turn(device, preroll_discard)
    total_ms = int((time.monotonic() - t0) * 1000)
    device_id = getattr(device, "device_id", "unknown")
    if end_reason in ("no_speech", "cancelled") or not pcm:
        await _record_turn(device, {
            "ts": time.time(), "trigger": trigger_label,
            "outcome": end_reason, "backend": "poc", "wake_score": wake_score,
            "total_ms": total_ms, "audio_ms": len(pcm) // 32,
            "stt_text": "", "decide_verdict": None,
            "decide_confidence": None, "engine": None, "actions": [],
        })
        # FORK: wake-wiring — the POC turn never ran, so nothing on the
        # POC side will resume the media this session_start paused: end
        # the session now so media returns in well under the POC's 90 s
        # auto-resume. Only this branch: stop turns must stay stopped,
        # and turns the POC did run own their resume via /api/turn.
        spawn_session_end(device)
        return False
    # Thinking transition AFTER capture: for private listening this closes
    # the session (listen_close); pre-closing would make on_thinking take
    # its mic_stop branch instead. Capture endpoints first, then think.
    await on_thinking()
    session_id = str(getattr(device, "listen_session", None) or uuid.uuid4())
    try:
        result = await post_turn(
            poc_endpoint(device), pcm, session_id, device_id,
            trigger_label == "barge-in", end_reason)
    except Exception as e:
        if allow_fallback and failure_replayable(e):
            log.info(f"[poc] {device_id}: POC never ran ({e}) — "
                     f"replaying {len(pcm)} bytes into HA fallback")
            outcome = await _replay_and_fallback(
                device, pcm, put_frame, sentinel_end, ha_turn)
            await _record_turn(device, {
                "ts": time.time(), "trigger": trigger_label,
                "outcome": "ha_fallback", "backend": "poc->ha",
                "wake_score": wake_score,
                "total_ms": int((time.monotonic() - t0) * 1000),
                "audio_ms": len(pcm) // 32,
                "stt_text": "", "decide_verdict": None,
                "decide_confidence": None, "engine": None, "actions": [],
            })
            return outcome
        # Ambiguous failure (timeout, 5xx, bad reply): the POC may already
        # have acted. NEVER replay — a replayed HA turn could double the
        # action. The turn ends quietly; the failure is in the log.
        log.warning(f"[poc] {device_id}: POC failed ambiguously ({e}) — "
                    f"ending turn quietly, no fallback replay")
        await _record_turn(device, {
            "ts": time.time(), "trigger": trigger_label,
            "outcome": "poc_failed", "backend": "poc", "wake_score": wake_score,
            "total_ms": int((time.monotonic() - t0) * 1000),
            "audio_ms": len(pcm) // 32,
            "stt_text": "", "decide_verdict": None,
            "decide_confidence": None, "engine": None, "actions": [],
        })
        return False
    if result.stop:
        # Unconditional halt: the barge path normally cancels playback at
        # turn start, but a stop turn that began without barge (device wake
        # without controller barge detection) must still silence any
        # in-progress audio. Best-effort: a flush failure must never break
        # the stop turn itself. cancel_event is cleared at next turn start.
        try:
            device.cancel_event.set()
            await device.send_control({"type": "speaker_flush"})
        except Exception as e:
            log.warning(f"[poc] {device_id}: stop flush failed ({e})")
        await _record_turn(device, {
            "ts": time.time(), "trigger": trigger_label,
            "outcome": "stop", "backend": "poc", "wake_score": wake_score,
            "total_ms": int((time.monotonic() - t0) * 1000),
            "audio_ms": len(pcm) // 32,
            "stt_text": result.transcript,
            "decide_verdict": result.decide_verdict,
            "decide_confidence": result.decide_confidence,
            "engine": result.engine, "actions": result.actions,
        })
        return False
    if not result.audio_pcm:
        await _record_turn(device, {
            "ts": time.time(), "trigger": trigger_label,
            "outcome": "no_reply", "backend": "poc", "wake_score": wake_score,
            "total_ms": int((time.monotonic() - t0) * 1000),
            "audio_ms": len(pcm) // 32,
            "stt_text": result.transcript,
            "decide_verdict": result.decide_verdict,
            "decide_confidence": result.decide_confidence,
            "engine": result.engine, "actions": result.actions,
        })
        return False
    played = await post_turn_play(_chunks(result.audio_pcm))
    await _record_turn(device, {
        "ts": time.time(), "trigger": trigger_label,
        "outcome": "barged" if device.cancel_event.is_set() else "ok",
        "backend": "poc", "wake_score": wake_score,
        "total_ms": int((time.monotonic() - t0) * 1000),
        "audio_ms": len(pcm) // 32,
        "playback_ms": played if isinstance(played, int) else -1,
        "stt_text": result.transcript,
        "decide_verdict": result.decide_verdict,
        "decide_confidence": result.decide_confidence,
        "engine": result.engine, "actions": result.actions,
    })
    return False
