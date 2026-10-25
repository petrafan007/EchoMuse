"""Mercury POC second sink (fork addition): router, capture, client rules.

Covers the acceptance load-bearing rules, not the POC server itself:
  - routing fails safe to stock HA behaviour on unknown values
  - capture endpoints on every sentinel, and never hangs
  - fallback replay happens ONLY when the POC demonstrably never ran
    (connection error / validation rejection) — timeouts and 5xx are
    ambiguous and must end quietly, never double-execute an action
  - POC reply framing is 48 kHz mono s16, nothing else

Async code is driven with asyncio.run, the same as test_listen.py — no
pytest-asyncio plugin. em_poc imports only stdlib + em_turnclock at top
level, so this file needs no controller heavy dependencies.
"""

import asyncio
import io
import struct
import wave

import pytest

import em_poc
import em_turnclock


# ─── Fakes ──────────────────────────────────────────────────────────────────

def loud_frame(n=1280, amp=8000):
    """80 ms of 16 kHz mono s16 speech-loud audio (RMS ~0.24)."""
    return struct.pack(f"<{n}h", *([amp] * n))


SILENCE_FRAME = struct.pack("<1280h", *([0] * 1280))


class FakeDevice:
    def __init__(self):
        self.voice_queue = asyncio.Queue()
        self.cancel_event = asyncio.Event()
        self.noise_floor = 0.0
        self.device_id = "test-dot"
        self.turn_history = []
        self.last_wake = {"score": 0.9}
        self.listen_session = None


def run(coro):
    return asyncio.run(coro)


async def _feed(device, items):
    for item in items:
        await device.voice_queue.put(item)


# ─── Routing ────────────────────────────────────────────────────────────────

def test_route_defaults_to_ha(monkeypatch):
    monkeypatch.delenv("EM_VOICE_ROUTE", raising=False)
    assert em_poc.resolve_route(FakeDevice()) == "ha"


def test_route_reads_device_attr_then_env(monkeypatch):
    monkeypatch.setenv("EM_VOICE_ROUTE", "poc_only")
    dev = FakeDevice()
    assert em_poc.resolve_route(dev) == "poc_only"
    dev.voice_route = "shadow"
    assert em_poc.resolve_route(dev) == "shadow"


def test_route_unknown_value_fails_safe_to_ha():
    dev = FakeDevice()
    dev.voice_route = "definitely-not-a-route"
    assert em_poc.resolve_route(dev) == "ha"


def test_route_echomuse_mercury_resolves_and_old_name_fails_safe():
    # Rename: poc_fallback -> echomuse_mercury (display "EchoMuse Mercury").
    assert "echomuse_mercury" in em_poc.ROUTES
    assert "poc_fallback" not in em_poc.ROUTES
    dev = FakeDevice()
    dev.voice_route = "echomuse_mercury"
    assert em_poc.resolve_route(dev) == "echomuse_mercury"
    dev.voice_route = "poc_fallback"
    assert em_poc.resolve_route(dev) == "ha"
    assert em_poc.ROUTES == ("ha", "echomuse_mercury", "poc_only", "shadow")


def test_poc_endpoint_default_and_validation(monkeypatch):
    monkeypatch.delenv("EM_POC_ENDPOINT", raising=False)
    dev = FakeDevice()
    assert em_poc.poc_endpoint(dev) == "http://127.0.0.1:8896"
    dev.poc_endpoint = "ftp://example.invalid/"
    assert em_poc.poc_endpoint(dev) == "http://127.0.0.1:8896"
    dev.poc_endpoint = "http://192.168.1.253:8896/"
    assert em_poc.poc_endpoint(dev) == "http://192.168.1.253:8896"


# ─── RMS speech bar ─────────────────────────────────────────────────────────

def test_rms_speech_bar():
    assert em_poc.rms_speech(loud_frame(), 0.0) is True
    assert em_poc.rms_speech(SILENCE_FRAME, 0.0) is False
    # 3x-floor bar: a frame at 2x the floor is not speech.
    assert em_poc.rms_speech(loud_frame(amp=100), 0.01) is False
    assert em_poc.rms_speech(loud_frame(amp=2000), 0.01) is True


# ─── Capture ────────────────────────────────────────────────────────────────

def test_capture_vad_end_and_preroll():
    dev = FakeDevice()
    # Preroll discards the first two frames (wake-word tail), keeps the rest.
    frames = [loud_frame(), loud_frame(), loud_frame(), loud_frame()]
    pcm, reason = run(_capture(dev, frames, preroll=2))
    assert reason == "vad_end"
    assert pcm == loud_frame() * 2


def test_capture_unknown_sentinel_ends_turn():
    dev = FakeDevice()
    pcm, reason = run(_capture(dev, [loud_frame(), "something-new"]))
    assert reason == "vad_end"
    assert pcm == loud_frame()


def test_capture_device_no_speech_sentinel():
    dev = FakeDevice()
    pcm, reason = run(_capture(dev, [SILENCE_FRAME, "vad_no_speech_timeout"]))
    assert reason == "no_speech"


def test_capture_silence_endpoint_after_speech():
    dev = FakeDevice()
    # Speech, then silence: the ~1 s backstop is shortened here so the
    # test runs fast; the rule (silence after speech ends the turn) is
    # what is pinned, not the constant.
    pcm, reason = run(_capture(dev, [loud_frame()] + [SILENCE_FRAME] * 8,
                               silence_end=0.2))
    assert reason == "vad_end"
    assert pcm.startswith(loud_frame())


def test_capture_cancelled():
    dev = FakeDevice()
    dev.cancel_event.set()
    pcm, reason = run(_capture(dev, []))
    assert reason == "cancelled"


def test_capture_max_open():
    dev = FakeDevice()
    pcm, reason = run(_capture(dev, [], max_turn=0.3))
    assert reason == "max_open"


def test_capture_no_speech_verdict(monkeypatch):
    # Pin the verdict, not the 5 s clock: silence from the first frame
    # must close quietly once the verdict says so.
    dev = FakeDevice()
    real = em_turnclock.no_speech_verdict

    def _fast(now, turn_start, listening_since, speech_seen):
        if listening_since is not None and not speech_seen:
            return True, "test no-speech"
        return real(now, turn_start, listening_since, speech_seen)

    monkeypatch.setattr(em_turnclock, "no_speech_verdict", _fast)
    pcm, reason = run(_capture(dev, [SILENCE_FRAME] * 3))
    assert reason == "no_speech"


async def _capture(dev, items, preroll=0, silence_end=None, max_turn=30.0):
    async def _feed_then():
        await _feed(dev, items)
    feeder = asyncio.create_task(_feed_then())
    try:
        return await em_poc.capture_turn(
            dev, preroll,
            silence_end=silence_end if silence_end is not None else 1.0,
            max_turn=max_turn)
    finally:
        feeder.cancel()


# ─── Reply framing ──────────────────────────────────────────────────────────

def _wav(pcm, rate):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def test_wav_roundtrip_48k():
    pcm = loud_frame()
    assert em_poc.parse_wav48k(_wav(pcm, 48000)) == pcm


def test_wav_wrong_rate_rejected():
    with pytest.raises(em_poc.PocError):
        em_poc.parse_wav48k(_wav(loud_frame(), 16000))
    with pytest.raises(em_poc.PocError):
        em_poc.parse_wav48k(b"not a wav at all")


def test_build_wav_is_16k_mono():
    data = em_poc.build_wav(loud_frame())
    with wave.open(io.BytesIO(data), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)


# ─── Fallback classification (the no-double-answer rule) ────────────────────

def test_failure_replayable_only_when_poc_never_ran():
    assert em_poc.failure_replayable(em_poc.PocConnectionError("x")) is True
    assert em_poc.failure_replayable(em_poc.PocRejected("x")) is True
    assert em_poc.failure_replayable(em_poc.PocTimeout("x")) is False
    assert em_poc.failure_replayable(em_poc.PocError("x")) is False
    assert em_poc.failure_replayable(RuntimeError("x")) is False


# ─── run_poc_turn ───────────────────────────────────────────────────────────

def _poc_result(**kw):
    base = dict(transcript="hello", decide_verdict="question",
                decide_confidence=0.9, actions=[], engine="piper",
                stop=False, audio_pcm=b"")
    base.update(kw)
    return em_poc.PocResult(**base)


async def _run_turn(dev, allow_fallback, ha_turn, played,
                    feed_items):
    async def on_thinking():
        pass

    async def post_turn_play(chunks):
        n = 0
        async for c in chunks:
            played.append(c)
            n += len(c)
        return n

    # The feeder must be created inside the running loop, before the
    # capture starts consuming — creating it in sync context would fail
    # with "no running event loop".
    feeder = asyncio.create_task(_feed(dev, feed_items))
    try:
        return await em_poc.run_poc_turn(
            dev, on_thinking=on_thinking, post_turn_play=post_turn_play,
            trigger_label="wakeword(0.9)", preroll_discard=0,
            ha_turn=ha_turn, put_frame=dev.voice_queue.put,
            sentinel_end="vad_end", allow_fallback=allow_fallback)
    finally:
        feeder.cancel()


def _turn_with_audio(monkeypatch, dev, result_or_exc, allow_fallback,
                     ha_turn=None):
    async def fake_post_turn(*args, **kwargs):
        if isinstance(result_or_exc, Exception):
            raise result_or_exc
        return result_or_exc

    monkeypatch.setattr(em_poc, "post_turn", fake_post_turn)
    played, ha_calls = [], []
    if ha_turn is None:
        async def ha_turn():
            ha_calls.append(True)
            return False
    outcome = run(_run_turn(
        dev, allow_fallback, ha_turn, played,
        [loud_frame(), loud_frame(), "vad_end"]))
    return outcome, ha_calls, played


def test_poc_turn_stop_never_plays(monkeypatch):
    dev = FakeDevice()
    outcome, ha_calls, played = _turn_with_audio(
        monkeypatch, dev, _poc_result(stop=True, audio_pcm=b""), True)
    assert outcome is False
    assert ha_calls == [] and played == []
    assert dev.turn_history[-1]["outcome"] == "stop"
    assert dev.turn_history[-1]["backend"] == "poc"


def test_poc_turn_ok_plays_reply(monkeypatch):
    dev = FakeDevice()
    reply = loud_frame() * 10
    outcome, ha_calls, played = _turn_with_audio(
        monkeypatch, dev, _poc_result(audio_pcm=reply), True)
    assert outcome is False
    assert ha_calls == []
    assert b"".join(played) == reply
    assert dev.turn_history[-1]["outcome"] == "ok"


def test_poc_turn_connection_failure_replays_to_ha(monkeypatch):
    dev = FakeDevice()
    drained = []

    async def ha_turn_drain():
        # Stands in for _stream_mic_audio: consumes the replayed frames
        # until the sentinel, the way the real HA turn would.
        while True:
            item = await dev.voice_queue.get()
            drained.append(item)
            if item == "vad_end":
                break
        return False

    outcome, ha_calls, played = _turn_with_audio(
        monkeypatch, dev, em_poc.PocConnectionError("refused"), True,
        ha_turn=ha_turn_drain)
    assert outcome is False
    assert dev.turn_history[-1]["outcome"] == "ha_fallback"
    assert b"".join(b for b in drained if isinstance(b, bytes)) == loud_frame() * 2
    assert drained[-1] == "vad_end"


def test_poc_turn_timeout_never_replays(monkeypatch):
    dev = FakeDevice()
    outcome, ha_calls, played = _turn_with_audio(
        monkeypatch, dev, em_poc.PocTimeout("slow"), True)
    assert ha_calls == []
    assert dev.turn_history[-1]["outcome"] == "poc_failed"


def test_poc_only_never_falls_back_even_on_connection_error(monkeypatch):
    dev = FakeDevice()
    outcome, ha_calls, played = _turn_with_audio(
        monkeypatch, dev, em_poc.PocConnectionError("refused"), False)
    assert ha_calls == []
    assert dev.turn_history[-1]["outcome"] == "poc_failed"


def test_poc_turn_no_speech_never_calls_poc(monkeypatch):
    dev = FakeDevice()
    called = []

    async def fake_post_turn(*args, **kwargs):
        called.append(True)
        return _poc_result()

    monkeypatch.setattr(em_poc, "post_turn", fake_post_turn)
    played, ha_calls = [], []

    async def ha_turn():
        ha_calls.append(True)
        return False

    outcome = run(_run_turn(
        dev, True, ha_turn, played,
        [SILENCE_FRAME, "vad_no_speech_timeout"]))
    assert outcome is False
    assert called == [] and ha_calls == []
    assert dev.turn_history[-1]["outcome"] == "no_speech"


# ─── post_turn HTTP layer (stdlib server, aiohttp client) ───────────────────

import base64 as _b64
import json as _json
import threading as _threading
import time as _time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _serve(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    _threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _handler_for(status, body, seen=None, delay=0.0):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if seen is not None:
                seen.append(_json.loads(raw) if raw else {})
            if delay:
                _time.sleep(delay)
            data = body if isinstance(body, bytes) else _json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return H


def _reply_body(pcm=None, **kw):
    body = {"transcript": "hello", "decide_verdict": "question",
            "decide_confidence": 0.9, "actions": [], "engine": "wavenet",
            "stop": False,
            "audio_b64": _b64.b64encode(_wav(pcm, 48000)).decode() if pcm else ""}
    body.update(kw)
    return body


def test_post_turn_success_and_request_shape():
    seen = []
    srv, url = _serve(_handler_for(200, _reply_body(loud_frame()), seen))
    try:
        result = run(em_poc.post_turn(url, loud_frame(), "s1", "test-dot",
                                      False, "vad_end", mode="shadow"))
    finally:
        srv.shutdown()
    assert result.transcript == "hello"
    assert result.decide_verdict == "question"
    assert result.engine == "wavenet"
    assert result.stop is False
    assert result.audio_pcm == loud_frame()
    req = seen[0]
    assert req["session_id"] == "s1" and req["device"] == "test-dot"
    assert req["end_reason"] == "vad_end" and req["mode"] == "shadow"
    with wave.open(io.BytesIO(_b64.b64decode(req["audio_b64"])), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)


def test_post_turn_stop_has_no_audio():
    srv, url = _serve(_handler_for(200, _reply_body(stop=True)))
    try:
        result = run(em_poc.post_turn(url, loud_frame(), "s1", "test-dot",
                                      False, "vad_end"))
    finally:
        srv.shutdown()
    assert result.stop is True and result.audio_pcm == b""


def test_post_turn_400_is_replayable_rejection():
    srv, url = _serve(_handler_for(400, {"error": "bad audio"}))
    try:
        with pytest.raises(em_poc.PocRejected):
            run(em_poc.post_turn(url, loud_frame(), "s1", "test-dot",
                                 False, "vad_end"))
    finally:
        srv.shutdown()


def test_post_turn_500_is_ambiguous_not_replayable():
    srv, url = _serve(_handler_for(500, {"error": "boom"}))
    try:
        with pytest.raises(em_poc.PocError) as exc:
            run(em_poc.post_turn(url, loud_frame(), "s1", "test-dot",
                                 False, "vad_end"))
        assert not em_poc.failure_replayable(exc.value)
    finally:
        srv.shutdown()


def test_post_turn_bad_json_is_ambiguous():
    srv, url = _serve(_handler_for(200, b"this is not json"))
    try:
        with pytest.raises(em_poc.PocError):
            run(em_poc.post_turn(url, loud_frame(), "s1", "test-dot",
                                 False, "vad_end"))
    finally:
        srv.shutdown()


def test_post_turn_wrong_rate_reply_is_refused():
    # A 16 kHz reply must be refused, never played at 48 kHz speed.
    body = _reply_body()
    body["audio_b64"] = _b64.b64encode(_wav(loud_frame(), 16000)).decode()
    srv, url = _serve(_handler_for(200, body))
    try:
        with pytest.raises(em_poc.PocError):
            run(em_poc.post_turn(url, loud_frame(), "s1", "test-dot",
                                 False, "vad_end"))
    finally:
        srv.shutdown()


def test_post_turn_timeout_is_ambiguous():
    srv, url = _serve(_handler_for(200, _reply_body(loud_frame()), delay=2.0))
    try:
        with pytest.raises(em_poc.PocTimeout) as exc:
            run(em_poc.post_turn(url, loud_frame(), "s1", "test-dot",
                                 False, "vad_end", timeout_s=0.2))
        assert not em_poc.failure_replayable(exc.value)
    finally:
        srv.shutdown()


def test_post_turn_unreachable_is_replayable():
    with pytest.raises(em_poc.PocConnectionError) as exc:
        run(em_poc.post_turn("http://127.0.0.1:1", loud_frame(), "s1",
                             "test-dot", False, "vad_end", timeout_s=2.0))
    assert em_poc.failure_replayable(exc.value)


# ─── Shadow tap ─────────────────────────────────────────────────────────────

def test_capped_tap_stops_at_cap():
    buf = bytearray()
    tap = em_poc.capped_tap(buf, cap=10)
    tap(b"12345678")
    tap(b"abcdefgh")
    assert bytes(buf) == b"12345678ab"
