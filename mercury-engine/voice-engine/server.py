#!/usr/bin/env python3
"""Mercury Engine: fresh direct voice client on thinclient.

No Home Assistant voice pipeline: no Wyoming containers, no HA Assist
pipeline, no Extended OpenAI Conversation, no conversation triggers.
HA is the device backend only, reached via its REST API (HA runs on this
same machine, so the base URL is localhost).

Pipeline: browser push-to-talk -> faster-whisper (local) -> Decide router
(local keyword pre-check, then Mercury Decide) -> Mercury 3 brain with HA
tools, or stop, or ask-Muse marker -> Piper TTS (local) or Google Cloud
TTS WaveNet (free tier) -> browser audio. /api/turn serves the same pipeline to the forked EchoMuse controller (48 kHz mono WAV replies, bytes only, never played server-side). mode=shadow on /api/turn transcribes + Decide only (no Mercury 3, no HA action, no TTS).

Keys are NEVER printed or logged. They live only in engine_config.json next
to this file, written by the Engine UI. All status endpoints report booleans
only, never key values.

Run: python3 server.py (via the mercury-engine systemd unit)
Listen: 0.0.0.0:8896 (LAN-only Engine UI; no auth, same trust model as
other LAN dashboards. Do not expose beyond the LAN.)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import struct
import subprocess
import tempfile
import sys
import time
import re
import ssl
import threading
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "engine_config.json"
UI_PATH = BASE_DIR / "engine_ui.html"
TRANSCRIBE_SCRIPT = BASE_DIR / "transcribe.py"
VOICES_DIR = BASE_DIR / "voices"
VENV_PY = BASE_DIR / ".venv" / "bin" / "python"
VENV_PIPER = BASE_DIR / ".venv" / "bin" / "piper"
INCEPTION_BASE = "https://api.inceptionlabs.ai/v1"
HA_BASE = "http://127.0.0.1:8123/api"
HA_MEDIA_PLAYER = "media_player.office_voice_assistant"
DECIDE_THRESHOLD = 0.85
GOOGLE_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
GOOGLE_VOICES = {
    "en-US-Wavenet-A": "Male",
    "en-US-Wavenet-B": "Male",
    "en-US-Wavenet-C": "Female",
    "en-US-Wavenet-D": "Male",
    "en-US-Wavenet-E": "Female",
    "en-US-Wavenet-F": "Female",
    "en-US-Wavenet-G": "Female",
    "en-US-Wavenet-H": "Female",
    "en-US-Wavenet-I": "Male",
    "en-US-Wavenet-J": "Male",
}
DEFAULT_GOOGLE_VOICE = "en-US-Wavenet-D"
CLIPS_DIR = BASE_DIR / "clips"

STOP_PHRASES = {
    "stop", "shut up", "be quiet", "quiet", "silence", "stop that",
    "stop the noise", "turn it off", "stop the alarm", "stop the timer",
    "stop playing", "stop the sound", "shut the fuck up",
}

TIMER_CANCEL_PHRASES = {
    "stop the timer", "cancel the timer", "stop my timer",
    "cancel my timer", "stop the office timer",
    "cancel the office timer",
}


FRAGMENT_SOCIAL_OK = {
    "thank you", "thanks", "okay", "ok", "yes", "no", "hello", "hi",
    "hey", "good", "great", "sure", "please",
}
_FRAGMENT_DEVICE_RE = re.compile(
    r"\b(lamp|lab|light|lights|vacuum|timer|speaker|switch)\b")


def is_fragment_transcript(lowered):
    """Fragmentary/garbled transcript that must fail open to Mercury.

    A transcript of <=2 words with no device word is a fragment (bare
    "off." clipped from a device-off sentence, or garbled "Shara!"):
    routing it through Decide risks a stop verdict (bare "off" scored
    stop 0.881 on Oct 24) or a chatty unknown-device reply. Exact stop
    phrases, timer-cancel phrases, ask-Muse prefixes, and complete
    social acknowledgements are NOT fragments and pass through.
    """
    if lowered in STOP_PHRASES or lowered in TIMER_CANCEL_PHRASES:
        return False
    if lowered in FRAGMENT_SOCIAL_OK:
        return False
    if lowered.startswith(("ask muse", "tell muse")):
        return False
    words = lowered.split()
    if len(words) == 0 or len(words) > 2:
        return False
    if _FRAGMENT_DEVICE_RE.search(lowered) is not None:
        return False
    return True


def stop_ha_media(ha_token):
    """Stop ALL HA-side playback for a stop turn.

    The fork stops its own reply-audio channel on stop=true, but HA-side
    playback (pink/white noise via script.play_sleep_sounds on
    media_player.office_voice_assistant, a 10-hour file) is a separate
    stream the fork cannot stop. Call this on EVERY stop route (keyword,
    Decide, /api/ask, /api/stop), AFTER the timer-cancel carve-out has
    had its chance. The sleep-sounds script is single-shot play_media
    (holds no state), so media_stop is the effective action;
    script.stop_sleep_sounds is called as well for parity with the HA
    script of that name. Returns action records for the turn log.
    """
    _st, _state = ha_get_state(HA_MEDIA_PLAYER, ha_token)
    _cur = _state.get("state") if isinstance(_state, dict) else None
    log.info("stop ha media: player state before stop: %s", _cur)
    actions = []
    st, _b = ha_service("media_player", "media_stop", ha_token,
                        {"entity_id": HA_MEDIA_PLAYER})
    actions.append({"type": "ha_call_service", "domain": "media_player",
                    "service": "media_stop",
                    "entity_id": HA_MEDIA_PLAYER, "http_status": st})
    sst, _b2 = ha_service("script", "stop_sleep_sounds", ha_token, {})
    actions.append({"type": "ha_call_service", "domain": "script",
                    "service": "stop_sleep_sounds",
                    "entity_id": "script.stop_sleep_sounds",
                    "http_status": sst})
    log.info("stop ha media -> media_stop HTTP %s, stop_sleep_sounds HTTP %s",
             st, sst)
    return actions


logging.basicConfig(
    filename=str(BASE_DIR / "poc.log"), level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mercury-engine")
_config_lock = threading.Lock()


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"inception_key": data.get("inception_key", ""),
                "ha_token": data.get("ha_token", ""),
                "google_api_key": data.get("google_api_key", ""),
                "groq_api_key": data.get("groq_api_key", ""),
                "voice_choice": data.get("voice_choice", "piper"),
                "google_voice_name": data.get("google_voice_name", DEFAULT_GOOGLE_VOICE) if data.get("google_voice_name") in GOOGLE_VOICES else DEFAULT_GOOGLE_VOICE}
    except Exception:
        return {"inception_key": "", "ha_token": "",
                "google_api_key": "", "groq_api_key": "", "voice_choice": "piper",
                "google_voice_name": DEFAULT_GOOGLE_VOICE}


def save_config(patch: dict) -> dict:
    with _config_lock:
        cfg = load_config()
        for key in ("inception_key", "ha_token", "google_api_key", "groq_api_key"):
            # Only a non-empty new value overwrites a stored key. An
            # empty or missing field (e.g. a masked placeholder left
            # untouched in the UI) must NEVER blank a stored key.
            val = patch.get(key)
            if isinstance(val, str) and val:
                cfg[key] = val
        if "voice_choice" in patch and patch["voice_choice"] in ("piper", "google"):
            cfg["voice_choice"] = patch["voice_choice"]
        if "google_voice_name" in patch and patch["google_voice_name"] in GOOGLE_VOICES:
            cfg["google_voice_name"] = patch["google_voice_name"]
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    log.info("config updated (values not logged)")
    return {"inception_key_set": bool(cfg["inception_key"]),
            "ha_token_set": bool(cfg["ha_token"]),
            "google_api_key_set": bool(cfg["google_api_key"]),
            "groq_api_key_set": bool(cfg["groq_api_key"]),
            "voice_choice": cfg["voice_choice"],
            "google_voice_name": cfg["google_voice_name"]}


def voice_file() -> Path | None:
    try:
        name = (VOICES_DIR / "voice.txt").read_text(
            encoding="utf-8").strip()
    except Exception:
        name = ""
    if name:
        cand = VOICES_DIR / name
        if cand.exists():
            return cand
    for cand in sorted(VOICES_DIR.glob("*.onnx")):
        return cand
    return None


def read_json_body(handler, limit: int = 5 * 1024 * 1024) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0 or length > limit:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def post_json(url: str, token: str, payload: dict, timeout: int = 20):
    req = urllib.request.Request(url, method="POST",
                                 data=json.dumps(payload).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
        log.info("POST %s -> HTTP %s", url.split("/v1")[-1], exc.code)
        return exc.code, {"error": body[:200]}
    except Exception as exc:
        log.info("POST failed: %s", type(exc).__name__)
        return 0, {"error": type(exc).__name__}


def ha_service(domain: str, service: str, token: str, data: dict):
    url = f"{HA_BASE}/services/{domain}/{service}"
    return post_json(url, token, data, timeout=15)


def ha_get_state(entity_id: str, token: str):
    url = f"{HA_BASE}/states/{entity_id}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception:
        return 0, {}


def google_tts(text: str, api_key: str, voice_name: str = DEFAULT_GOOGLE_VOICE):
    """Google Cloud TTS, WaveNet voice only. Returns MP3 bytes or None.

    Never logs the key, the request URL, or the response body. Any
    failure returns None so the caller can fall back to local Piper.
    """
    payload = {
        "input": {"text": text},
        "voice": {"languageCode": "en-US", "name": voice_name if voice_name in GOOGLE_VOICES else DEFAULT_GOOGLE_VOICE},
        "audioConfig": {"audioEncoding": "MP3"},
    }
    req = urllib.request.Request(
        GOOGLE_TTS_URL + "?key=" + api_key, method="POST",
        data=json.dumps(payload).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        audio_b64 = body.get("audioContent", "")
        if not audio_b64:
            return None
        return base64.b64decode(audio_b64)
    except Exception as exc:
        log.info("google tts failed: %s", type(exc).__name__)
        return None


def decide_route(text: str, key: str) -> dict:
    """Mercury Decide router. Returns {route, confidence, source}.

    Shape verified live Oct 24, 2026 against Inception (mirrors
    VisaSentinel's working jev.ts transport): POST /v1/decisions with
    {model, state: object, questions: keyed object}. Boolean questions
    use type "noul" + "instructions"; choice questions use "criteria"
    (name -> description). Response answers are keyed by question name.
    The old shape (state as bare string, questions as array) 422s with
    "questions: Input should be a valid dictionary". On any Decide error
    we fail OPEN to Mercury 3 (never guess stop from a failed call).
    Extra return keys (request, probabilities, stop_confidence) feed the
    voice-turn event log; existing callers use route/confidence/source.
    """
    payload = {
        "model": "mercury-decide",
        "state": {"transcript": text},
        "questions": {
            "stop_audio": {
                "type": "noul",
                "instructions": ("Is the user EXPLICITLY asking to stop audio that is "
                    "currently playing (stop, shut up, be quiet, silence, "
                    "turn it off, end playback)? A request to START, CHOOSE, "
                    "or LISTEN TO music, sounds, or noise (play pink noise, "
                    "pick noise, play music) is NOT a stop. Turning a smart-home device off (turn the lamp, lab, or light off) is NOT stopping audio; that is home control."),
            },
            "request_kind": {
                "type": "choice",
                "instructions": "What kind of request is this?",
                "criteria": {
                    "stop": ("The user EXPLICITLY wants audio playback to stop "
                    "(stop, shut up, be quiet, turn it off). Requests to "
                    "play, pick, choose, or listen to sounds, music, or "
                    "noise are NOT stop; they are home_control. Turning a device off is home_control, not stop."),
                    "home_control": "The user wants to control a smart home device.",
                    "question": "The user is asking a general knowledge question.",
                    "ask_muse": "The user is asking to ask Muse something.",
                },
            },
        },
    }
    status, body = post_json(INCEPTION_BASE + "/decisions", key, payload,
                             timeout=20)
    log.info("decide /decisions -> HTTP %s", status)
    if status == 200 and isinstance(body, dict):
        try:
            answers = body.get("answers") or {}
            stop_ans = answers.get("stop_audio") or {}
            stop_conf = float(stop_ans.get("noul",
                                           stop_ans.get("probability", 0.0)))
            kind_ans = answers.get("request_kind") or {}
            choice = kind_ans.get("choice", "")
            probs = kind_ans.get("probabilities") or {}
            choice_conf = float(kind_ans.get("confidence",
                                             probs.get(choice, 0.0)))
            _lt = text.lower()
            _play_request = (
                re.search(r"\b(play|pick|choose|listen to|put on)\b", _lt) is not None
                and re.search(r"\b(noise|music|sound|song|audio)\b", _lt) is not None)
            _device_word = (
                re.search(r"\b(lamp|lab|light|lights|vacuum|switch)\b", _lt) is not None)
            _device_off = (
                _device_word
                and (re.search(r"\boff\b", _lt) is not None
                     or re.search(r"\bstop\b", _lt) is not None)
                and re.search(r"\b(music|noise|sound|song|audio|playback|speaker)\b", _lt) is None)
            _veto_stop = _play_request or _device_off
            if _veto_stop and stop_conf >= DECIDE_THRESHOLD:
                log.info("decide stop vetoed (not an audio stop)")
            if stop_conf >= DECIDE_THRESHOLD and not _veto_stop:
                return {"route": "stop", "confidence": stop_conf,
                        "source": "decide", "request": payload,
                        "probabilities": probs,
                        "stop_confidence": stop_conf}
            if choice and choice_conf >= DECIDE_THRESHOLD:
                return {"route": choice, "confidence": choice_conf,
                        "source": "decide", "request": payload,
                        "probabilities": probs,
                        "stop_confidence": stop_conf}
            return {"route": "mercury3", "confidence": choice_conf,
                    "source": "decide-unsure", "request": payload,
                    "probabilities": probs, "stop_confidence": stop_conf}
        except Exception:
            return {"route": "mercury3", "confidence": 0.0,
                    "source": "decide-parse-fallback", "request": payload,
                    "probabilities": {}, "stop_confidence": 0.0}
    return {"route": "mercury3", "confidence": 0.0, "source": "decide-error",
            "request": payload, "probabilities": {},
            "stop_confidence": 0.0}


VOICE_LOG_DIR = Path("/home/user/voice-logs")
VOICE_LOG_PATH = VOICE_LOG_DIR / "poc-turns.jsonl"
MERCURY_LAST = {"data": None}

_SECRET_KEY_RE = re.compile(
    r"(key|token|secret|password|auth|psk|bearer|credential)",
    re.IGNORECASE)


def _dur(t_end, t_start):
    if t_end is None or t_start is None:
        return None
    return round((t_end - t_start) * 1000, 1)


def _redact_value(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in ("audio_b64", "audioContent"):
                continue
            if _SECRET_KEY_RE.search(str(k)):
                out[k] = "<redacted>"
            else:
                out[k] = _redact_value(v)
        return out
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, str) and len(value) > 20000:
        return value[:20000] + "...<truncated>"
    return value


def emit_voice_event(event):
    """Append one redacted turn event to poc-turns.jsonl. Never raises:
    logging must never break, delay, or alter a voice turn."""
    try:
        VOICE_LOG_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(_redact_value(event), default=str)
        with open(VOICE_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
    except Exception:
        pass


def ha_list_states(token: str):
    url = f"{HA_BASE}/states"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return 200, data if isinstance(data, list) else []
    except urllib.error.HTTPError as exc:
        return exc.code, []
    except Exception:
        return 0, []


_INVENTORY_DOMAINS = ("light", "switch", "media_player", "vacuum",
                      "timer", "script")
_INVENTORY_EXCLUDE_RE = re.compile(
    r"_(detect|motion|recordings|snapshots|review_alerts|review_detections)$")


def build_entity_inventory(token: str) -> str:
    """Compact REAL entity inventory for the Mercury prompt.

    Frigate camera toggles ( *_detect/_motion/_recordings/_snapshots/
    *_review_* ) are excluded: they are not voice targets and would
    bloat the prompt. Everything else in the voice-relevant domains
    is listed verbatim from HA, so Mercury never has to guess an ID.
    """
    st, states = ha_list_states(token)
    if st != 200:
        return ""
    lines = []
    for s in states:
        eid = s.get("entity_id", "")
        dom = eid.split(".", 1)[0] if "." in eid else ""
        if dom not in _INVENTORY_DOMAINS:
            continue
        if dom == "switch" and _INVENTORY_EXCLUDE_RE.search(eid):
            continue
        friendly = (s.get("attributes") or {}).get("friendly_name", "")
        lines.append("%s (%s) = %s" % (eid, friendly, s.get("state"))
                     if friendly else "%s = %s" % (eid, s.get("state")))
    return ", ".join(sorted(lines))


def guard_done_claim(reply_text: str, actions) -> str:
    """Deterministic Done-grounding guard for the live /api/turn path.

    If Mercury claims completion ("Done." or equivalents) but ZERO
    ha_call_service calls succeeded in the turn, the claim is false:
    replace it with an honest failure message naming what was tried.
    Replies after a real service success pass through untouched, as
    do stop turns (no reply) and ask_muse turns (separate branch).
    """
    if not reply_text:
        return reply_text
    s = reply_text.strip().lower()
    claims = (s.startswith("done") or "all done" in s
              or "it is done" in s or "i have done" in s
              or s.startswith("completed"))
    if not claims:
        return reply_text
    ok = any(a.get("type") == "ha_call_service"
             and a.get("http_status") in (200, 201)
             for a in (actions or []))
    if ok:
        return reply_text
    tried = []
    for a in (actions or []):
        if a.get("type") == "ha_get_state" and a.get("http_status") == 404:
            tried.append(a.get("entity_id", ""))
        elif a.get("type") == "ha_call_service"                 and a.get("http_status") not in (200, 201):
            tried.append("%s.%s" % (a.get("domain", ""),
                                     a.get("service", "")))
    tried = sorted(set(t for t in tried if t))
    if tried:
        detail = ("I tried %s but that device was not found, "
                  "so nothing changed." % ", ".join(tried))
    else:
        detail = "No device action succeeded, so nothing changed."
    return ("I could not do that. " + detail
            + " Please say the device name again if I misheard it.")



_MEDIA_PAUSE = {"paused": False, "session_id": None, "generation": 0,
                "timer": None}
_MEDIA_PAUSE_LOCK = threading.Lock()
_MEDIA_PAUSE_AUTO_RESUME_S = 90.0


def _clear_pause_timer_locked():
    t = _MEDIA_PAUSE["timer"]
    if t is not None:
        try:
            t.cancel()
        except Exception:
            pass
    _MEDIA_PAUSE["timer"] = None


def _do_resume_locked(ha_token, reason):
    st, _b = ha_service("media_player", "media_play", ha_token,
                        {"entity_id": HA_MEDIA_PLAYER})
    _clear_pause_timer_locked()
    _MEDIA_PAUSE["paused"] = False
    _MEDIA_PAUSE["session_id"] = None
    log.info("media resumed (%s) -> HA HTTP %s", reason, st)
    return st in (200, 201)


def _delayed_resume_media(generation, ha_token):
    with _MEDIA_PAUSE_LOCK:
        if not _MEDIA_PAUSE["paused"]:
            return
        if _MEDIA_PAUSE["generation"] != generation:
            return
        _do_resume_locked(ha_token, "delayed-after-reply")


def _auto_resume_media(generation, ha_token):
    with _MEDIA_PAUSE_LOCK:
        if not _MEDIA_PAUSE["paused"]:
            return
        if _MEDIA_PAUSE["generation"] != generation:
            return
        _do_resume_locked(ha_token, "auto-resume-no-turn")


def pause_media_for_session(session_id, ha_token):
    """Pause HA media at voice-session start; record that WE paused it.

    Only pauses when the player is actually playing, so a later resume
    never restarts media the user paused themselves. Starts a 90 s
    auto-resume timer in case no turn ever arrives (fork no_speech end).
    """
    with _MEDIA_PAUSE_LOCK:
        _st, _state = ha_get_state(HA_MEDIA_PLAYER, ha_token)
        cur = _state.get("state") if isinstance(_state, dict) else None
        if cur != "playing":
            log.info("session start: player not playing (%s), no pause", cur)
            return {"paused": False, "state_before": cur, "http_status": None}
        pst, _b = ha_service("media_player", "media_pause", ha_token,
                             {"entity_id": HA_MEDIA_PLAYER})
        if pst not in (200, 201):
            log.info("session start: media_pause failed HTTP %s", pst)
            return {"paused": False, "state_before": cur, "http_status": pst}
        _MEDIA_PAUSE["generation"] += 1
        gen = _MEDIA_PAUSE["generation"]
        _clear_pause_timer_locked()
        _MEDIA_PAUSE["paused"] = True
        _MEDIA_PAUSE["session_id"] = session_id
        t = threading.Timer(_MEDIA_PAUSE_AUTO_RESUME_S,
                            _auto_resume_media, args=(gen, ha_token))
        t.daemon = True
        t.start()
        _MEDIA_PAUSE["timer"] = t
        log.info("session start: media paused (session %s)", session_id or "?")
        return {"paused": True, "state_before": cur, "http_status": pst}


def resume_media_now(ha_token, reason):
    """Resume immediately, but only if this Engine paused the media."""
    with _MEDIA_PAUSE_LOCK:
        if not _MEDIA_PAUSE["paused"]:
            return False
        return _do_resume_locked(ha_token, reason)


def schedule_media_resume(ha_token, delay_s):
    """Resume after delay_s (reply duration + buffer), generation-checked."""
    with _MEDIA_PAUSE_LOCK:
        if not _MEDIA_PAUSE["paused"]:
            return
        gen = _MEDIA_PAUSE["generation"]
        _clear_pause_timer_locked()
        t = threading.Timer(delay_s, _delayed_resume_media,
                            args=(gen, ha_token))
        t.daemon = True
        t.start()
        _MEDIA_PAUSE["timer"] = t
        log.info("media resume scheduled in %.1fs", delay_s)


def cancel_paused_media():
    """Drop the paused flag WITHOUT resuming (media-stop turns)."""
    with _MEDIA_PAUSE_LOCK:
        if not _MEDIA_PAUSE["paused"]:
            return
        _clear_pause_timer_locked()
        _MEDIA_PAUSE["paused"] = False
        _MEDIA_PAUSE["session_id"] = None
        log.info("paused-media flag cleared without resume (stop turn)")


def _turn_took_over_media(actions):
    for a in (actions or []):
        if a.get("type") != "ha_call_service":
            continue
        if a.get("domain") == "media_player":
            return True
        if a.get("domain") == "script" and a.get("service") in (
                "play_sleep_sounds", "stop_sleep_sounds"):
            return True
    return False


def try_timer_cancel(lowered: str, ha_token: str):
    """Narrow timer-cancel carve-out.

    Returns (handled, actions). Only for the exact timer-cancel
    phrases, and only when timer.office_timer is actually active
    or paused: it cancels the timer and the caller ends the turn
    silently (no media_stop, no spoken reply). When the timer is
    idle it returns (False, []) so normal stop handling applies.
    """
    if lowered not in TIMER_CANCEL_PHRASES:
        return False, []
    st, state = ha_get_state("timer.office_timer", ha_token)
    cur = state.get("state") if isinstance(state, dict) else None
    if cur not in ("active", "paused"):
        return False, []
    cst, _b = ha_service("timer", "cancel", ha_token,
                         {"entity_id": "timer.office_timer"})
    actions = [{"type": "ha_call_service", "domain": "timer",
                "service": "cancel",
                "entity_id": "timer.office_timer",
                "http_status": cst}]
    return True, actions


HA_TOOLS = [
    {"type": "function",
     "function": {"name": "ha_get_state",
                  "description": "Read the current state of a Home Assistant entity.",
                  "parameters": {"type": "object",
                                 "properties": {"entity_id": {"type": "string"}},
                                 "required": ["entity_id"]}}},
    {"type": "function",
     "function": {"name": "ha_call_service",
                  "description": "Call a Home Assistant service for a device.",
                  "parameters": {"type": "object",
                                 "properties": {
                                     "domain": {"type": "string"},
                                     "service": {"type": "string"},
                                     "entity_id": {"type": "string"},
                                     "data": {"type": "object"}},
                                 "required": ["domain", "service"]}}},
    {"type": "function",
     "function": {"name": "ha_list_entities",
                  "description": ("List the REAL Home Assistant entities "
                                  "you may control. This inventory is "
                                  "authoritative: use ONLY entity IDs "
                                  "from it, never guess or vary an ID."),
                  "parameters": {"type": "object", "properties": {}}}},
]


def mercury3_answer(text: str, key: str, ha_token: str,
                    actions=None, clarify_only=False) -> str:
    _now_et = datetime.now(ZoneInfo("America/New_York"))
    _clock = _now_et.strftime("%A, %B %d, %Y at %I:%M %p Eastern")
    _inventory = build_entity_inventory(ha_token)
    _inv_text = _inventory if _inventory else (
        "(inventory fetch failed; use ha_list_entities, and if a lookup "
        "returns 404 say the device was not found; never guess an ID)")
    messages = [
        {"role": "system",
         "content": (
             "You control the user's home via Home Assistant. "
             "Respond ONLY in English, always, no matter what language "
             "the transcript is in. "
             "Ask-Muse rule: if the user asks you to ask Muse "
             "something, reply with exactly Asking Muse. and nothing "
             "else -- no narration of the handoff, no sentence about "
             "a spoken answer following, no extra words. "
             "Today is " + _clock + ". Answer time and date questions "
             "from that clock; never say you have no clock and do not "
             "probe sensor.time (it does not exist). Time and date scope: if the user asks only for the time, reply with ONLY the time. If the user asks only for the date, reply with ONLY the date. If the user asks for both, give both. Never add the part that was not asked for. "
             "Use ha_get_state to read devices and ha_call_service "
             "to change them. REAL DEVICES (complete authoritative "
             "inventory; never invent, guess, or vary an entity ID; "
             "if a device is not listed here, say so by name and do "
             "not claim success): " + _inv_text + " "
             "Key mappings: the user sometimes calls the office the lab, so "
             "the lab lamp or lab light means switch.lamp. "
             "The lamp is switch.lamp (there are NO "
             "light.* entities; never use light.lab or any light.* ID); "
             "the only speaker is media_player.office_voice_assistant; "
             "pink or white noise plays ONLY via script.play_sleep_sounds "
             "(sound=pink or white) on that speaker, never by guessing "
             "a media_player.* ID; the timer is timer.office_timer, "
             "started with timer.start and a duration like 0:00:10 and "
             "cancelled with timer.cancel; the vacuum is vacuum.s8. "
             "Claim completion ONLY after a ha_call_service call "
             "returned success. If every lookup failed or nothing "
             "succeeded, say exactly what failed and do not say Done. "
             "Answer briefly, one or two sentences. Simple device command "
             "rule: when the user asked for a simple device action (such as "
             "turning a device on or off), the action succeeded via "
             "ha_call_service, and there is nothing else to tell the user "
             "(no information to convey, no question, no failure), reply "
             "with exactly __ACK__ and nothing else -- no words, no "
             "punctuation, no explanation. The __ACK__ reply is replaced "
             "by a short confirmation ding and is never spoken aloud. For "
             "any other turn (questions, information, failures, "
             "clarifications), reply normally in words. Ask-Muse turns are "
             "the exception: reply with exactly Asking Muse. and "
             "nothing else.")},
        {"role": "user", "content": text},
    ]
    if clarify_only:
        messages[0] = {"role": "system", "content": messages[0]["content"]
                       + " The transcript is fragmentary or garbled: ask ONE short "
                       "clarification question about what was meant; take NO device "
                       "action, guess NO device name, and keep the reply to one sentence."}
    tool_call_log = []
    for _round in range(3):
        status, body = post_json(
            INCEPTION_BASE + "/chat/completions", key,
            {"model": "mercury-3", "messages": messages,
             "tools": [] if clarify_only else HA_TOOLS}, timeout=60)
        log.info("mercury-3 -> HTTP %s", status)
        if status != 200:
            MERCURY_LAST["data"] = {
                "messages": messages, "tool_calls": tool_call_log,
                "error": "api_error", "http_status": status}
            return "Mercury 3 did not answer (API error, status logged)."
        try:
            msg = body["choices"][0]["message"]
        except Exception:
            MERCURY_LAST["data"] = {
                "messages": messages, "tool_calls": tool_call_log,
                "error": "unexpected_reply"}
            return "Mercury 3 returned an unexpected reply."
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            MERCURY_LAST["data"] = {
                "messages": messages, "tool_calls": tool_call_log,
                "reply_text": msg.get("content") or "(no reply)"}
            return msg.get("content") or "(no reply)"
        messages.append(msg)
        for call in tool_calls:
            try:
                fn = call["function"]["name"]
                args = json.loads(call["function"].get("arguments") or "{}")
            except Exception:
                continue
            if fn == "ha_get_state":
                st, state = ha_get_state(args.get("entity_id", ""), ha_token)
                result = {"http_status": st,
                          "state": state.get("state"),
                          "attributes": state.get("attributes", {})}
                if actions is not None:
                    actions.append({"type": "ha_get_state",
                                    "entity_id": args.get("entity_id", ""),
                                    "http_status": st})
            elif fn == "ha_list_entities":
                result = {"inventory": _inventory}
                if actions is not None:
                    actions.append({"type": "ha_list_entities",
                                    "http_status": 200 if _inventory else 0})
            elif fn == "ha_call_service":
                data = dict(args.get("data") or {})
                if args.get("entity_id"):
                    data["entity_id"] = args["entity_id"]
                st, _b = ha_service(args.get("domain", ""),
                                    args.get("service", ""), ha_token, data)
                result = {"http_status": st}
                if actions is not None:
                    actions.append({"type": "ha_call_service",
                                    "domain": args.get("domain", ""),
                                    "service": args.get("service", ""),
                                    "entity_id": args.get("entity_id", ""),
                                    "http_status": st})
            else:
                result = {"error": "unknown tool"}
            messages.append({"role": "tool",
                             "tool_call_id": call.get("id", ""),
                             "content": json.dumps(result)})
            tool_call_log.append({"name": fn, "args": args, "result": result})
    _any_success = any(
        tc.get("name") == "ha_call_service"
        and (tc.get("result") or {}).get("http_status") in (200, 201)
        for tc in tool_call_log)
    _final = ("Done." if _any_success else
              "I tried that but did not get a final confirmation from "
              "the device, so I cannot confirm it worked.")
    MERCURY_LAST["data"] = {"messages": messages,
                            "tool_calls": tool_call_log,
                            "reply_text": _final}
    return _final

def ask_muse_marker(question: str, ha_token: str) -> dict:
    text = question.strip()
    for prefix in ("ask muse to ", "ask muse ", "tell muse to ",
                   "tell muse "):
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip()
            break
    status, _body = ha_service("system_log", "write", ha_token,
                               {"message": "ASK_CLAUDE|" + text,
                                "level": "warning"})
    log.info("ask-muse marker -> HTTP %s", status)
    return {"marker_sent": status in (200, 201), "http_status": status}



def _wav_header_pcm16(data_len: int, sample_rate: int = 16000,
                      channels: int = 1) -> bytes:
    """44-byte RIFF header for raw mono S16_LE PCM (what the Dot sends)."""
    byte_rate = sample_rate * channels * 2
    block_align = channels * 2
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + data_len,
                       b"WAVE", b"fmt ", 16, 1, channels, sample_rate,
                       byte_rate, block_align, 16, b"data", data_len)


# ASR model + tuning (Oct 25 2026 benchmark: distil-small.en 25/25 adjusted
# vs small 22/25; in-process load removes the per-turn model-reload cost).
ASR_MODEL_NAME = "distil-small.en"
ASR_HOTWORDS = "lamp vacuum timer alarm Jarvis Muse"
_ASR_MODEL = None
_ASR_MODEL_LOCK = threading.Lock()


def get_asr_model():
    """Lazily load the shared faster-whisper model once (thread-safe)."""
    global _ASR_MODEL
    if _ASR_MODEL is None:
        with _ASR_MODEL_LOCK:
            if _ASR_MODEL is None:
                from faster_whisper import WhisperModel
                t0 = time.monotonic()
                _ASR_MODEL = WhisperModel(ASR_MODEL_NAME, device="cpu", compute_type="int8")
                log.info("asr model loaded: %s in %.1fs", ASR_MODEL_NAME, time.monotonic() - t0)
    return _ASR_MODEL


def transcribe_file(path: str) -> str:
    """In-process faster-whisper transcription (shared model, locked)."""
    model = get_asr_model()
    with _ASR_MODEL_LOCK:
        segments, _info = model.transcribe(
            path, language="en", vad_filter=True,
            vad_parameters={"threshold": 0.3},
            condition_on_previous_text=False,
            hotwords=ASR_HOTWORDS,
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0))
        return "".join(seg.text for seg in segments).strip()

# Groq cloud STT (prototype Oct 26 2026): cloud-first transcription with
# local distil-small fallback. The cloud attempt is capped so a slow cloud
# never makes a turn slower than local-only; after repeated failures the
# cloud is skipped for a cooldown window (circuit breaker). Keys are never
# logged; only failure kinds and HTTP status codes are.
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL = "whisper-large-v3-turbo"
GROQ_STT_TIMEOUT_S = 2.0
_GROQ_FAIL_THRESHOLD = 3
_GROQ_COOLDOWN_S = 300.0
_GROQ_STATE_LOCK = threading.Lock()
_GROQ_FAILS = 0
_GROQ_COOLDOWN_UNTIL = 0.0


def groq_transcribe(path, api_key):
    """One Groq transcription. Returns text, or None on ANY failure."""
    try:
        audio = Path(path).read_bytes()
    except Exception:
        return None
    boundary = uuid.uuid4().hex
    body = b""
    body += ("--" + boundary + "\r\n").encode()
    body += b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
    body += b"Content-Type: audio/wav\r\n\r\n"
    body += audio
    body += ("\r\n--" + boundary + "\r\n").encode()
    body += b'Content-Disposition: form-data; name="model"\r\n\r\n'
    body += GROQ_STT_MODEL.encode()
    body += ("\r\n--" + boundary + "\r\n").encode()
    body += b'Content-Disposition: form-data; name="response_format"\r\n\r\n'
    body += b"json"
    body += ("\r\n--" + boundary + "\r\n").encode()
    body += b'Content-Disposition: form-data; name="language"\r\n\r\n'
    body += b"en"
    body += ("\r\n--" + boundary + "--\r\n").encode()
    req = urllib.request.Request(GROQ_STT_URL, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + api_key)
    req.add_header("User-Agent", "Meta-Muse-engine/1.0")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    try:
        with urllib.request.urlopen(req, timeout=GROQ_STT_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code is not None:
            log.info("groq stt http status: %s", code)
        else:
            log.info("groq stt failed: %s", type(exc).__name__)
        return None
    text = (data.get("text") or "").strip()
    return text if text else None


def transcribe_with_fallback(path):
    """Cloud-first transcription. Returns (transcript, engine, reason).

    engine is "groq" or "local"; reason is None when Groq served, else
    "no_key", "cooldown", or "cloud_error". Cloud failures never raise;
    a local failure still raises and the caller handles it as before.
    """
    global _GROQ_FAILS, _GROQ_COOLDOWN_UNTIL
    cfg = load_config()
    key = cfg.get("groq_api_key", "")
    if not key:
        return transcribe_file(path), "local", "no_key"
    with _GROQ_STATE_LOCK:
        cooled = time.monotonic() < _GROQ_COOLDOWN_UNTIL
    if cooled:
        return transcribe_file(path), "local", "cooldown"
    text = groq_transcribe(path, key)
    if text is not None:
        with _GROQ_STATE_LOCK:
            _GROQ_FAILS = 0
        log.info("asr engine: groq")
        return text, "groq", None
    with _GROQ_STATE_LOCK:
        _GROQ_FAILS += 1
        if _GROQ_FAILS >= _GROQ_FAIL_THRESHOLD:
            _GROQ_COOLDOWN_UNTIL = time.monotonic() + _GROQ_COOLDOWN_S
            _GROQ_FAILS = 0
    log.info("asr engine: local (groq failed, falling back)")
    return transcribe_file(path), "local", "cloud_error"



def selected_engine(cfg: dict) -> str:
    return "wavenet" if cfg["voice_choice"] == "google" else "piper"


ACK_SENTINEL = "__ACK__"
DING_WAV_PATH = Path(__file__).resolve().parent / "ding.wav"


def load_ding_wav():
    """48 kHz mono WAV bytes for the confirmation ding, or None.

    The file is generated with ffmpeg (two-tone descending 880 Hz then
    659 Hz); a missing or malformed file returns None so the caller
    can fall back to a spoken confirmation instead of failing.
    """
    try:
        data = DING_WAV_PATH.read_bytes()
        if data[:4] == b"RIFF" and len(data) > 44:
            return data
    except Exception:
        pass
    return None


def synthesize_reply(text: str, cfg: dict):
    """(engine, raw_bytes) with the current voice + Piper fallback.

    Mirrors /api/tts exactly: Google WaveNet first when selected and a
    key is set, Piper on ANY Google failure. Raw bytes are MP3 for
    WaveNet, WAV (22050 Hz) for Piper; the caller transcodes to 48k.
    Uses a private temp file (never the shared reply.wav) so fork
    turns cannot race browser TTS. Returns (None, None) on failure.
    """
    if cfg["voice_choice"] == "google" and cfg["google_api_key"]:
        data = google_tts(text, cfg["google_api_key"],
                          cfg.get("google_voice_name", DEFAULT_GOOGLE_VOICE))
        if data:
            return "wavenet", data
        log.info("google tts unavailable, falling back to piper")
    vf = voice_file()
    if vf is None or not VENV_PIPER.exists():
        return None, None
    with tempfile.TemporaryDirectory() as td:
        out_wav = Path(td) / "reply.wav"
        subprocess.run([str(VENV_PIPER), "--model", str(vf),
                        "--output_file", str(out_wav)],
                       input=text.encode("utf-8"),
                       capture_output=True, timeout=60)
        if not out_wav.exists() or out_wav.stat().st_size == 0:
            return None, None
        return "piper", out_wav.read_bytes()


def transcode_wav48k(raw: bytes):
    """ffmpeg transcode (MP3 or WAV in) -> 48 kHz mono S16_LE WAV bytes.

    The Dot speaker path requires 48 kHz mono WAV passthrough; neither
    Piper (22050 Hz WAV) nor WaveNet (MP3) can go down it unconverted.
    Returns None on any ffmpeg failure (caller answers 500, never
    returns unconverted audio).
    """
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "in.audio"
        out = Path(td) / "out.wav"
        inp.write_bytes(raw)
        proc = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(inp),
             "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(out)],
            capture_output=True, timeout=60)
        if proc.returncode != 0 or not out.exists() \
                or out.stat().st_size == 0:
            log.info("ffmpeg transcode failed rc=%s", proc.returncode)
            return None
        return out.read_bytes()


# ---- Voice command matcher (Commands tab on prompts.petrafied.net) ----
MATCHER_RULES_PATH = BASE_DIR / "matcher_rules.json"
_MATCHER_LOCK = threading.Lock()
_MATCHER_CACHE = {"mtime": None, "data": None}
_MATCHER_DO_DOMAINS = {"light", "switch", "vacuum", "timer", "script", "lock", "input_datetime", "input_boolean"}
_MATCHER_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "twenty-one": 21, "twenty-two": 22,
    "twenty-three": 23, "twenty-four": 24, "twenty-five": 25,
    "twenty-six": 26, "twenty-seven": 27, "twenty-eight": 28,
    "twenty-nine": 29, "thirty": 30, "thirty-one": 31, "thirty-two": 32,
    "thirty-three": 33, "thirty-four": 34, "thirty-five": 35,
    "thirty-six": 36, "thirty-seven": 37, "thirty-eight": 38,
    "thirty-nine": 39, "forty": 40, "forty-one": 41, "forty-two": 42,
    "forty-three": 43, "forty-four": 44, "forty-five": 45,
    "forty-six": 46, "forty-seven": 47, "forty-eight": 48,
    "forty-nine": 49, "fifty": 50, "fifty-one": 51, "fifty-two": 52,
    "fifty-three": 53, "fifty-four": 54, "fifty-five": 55,
    "fifty-six": 56, "fifty-seven": 57, "fifty-eight": 58,
    "fifty-nine": 59, "sixty": 60,
}
_MATCHER_CONDITION_WORDS = {
    "clear-night": "clear", "clear-day": "clear", "sunny": "sunny",
    "partlycloudy": "partly cloudy", "mostlycloudy": "mostly cloudy",
    "cloudy": "cloudy", "rainy": "rainy", "pouring": "pouring rain",
    "snowy": "snowy", "snowy-rainy": "snowy rain", "windy": "windy",
    "fog": "foggy", "hail": "hail", "lightning": "lightning",
}
_MATCHER_CAMERA_NAMES = {
    "front door": "front_door", "garage": "garage",
    "patio": "patio", "driveway": "driveway",
}


def matcher_norm(text):
    t = (text or "").lower().strip()
    t = re.sub(r"\s+", " ", t)
    t = t.rstrip(".!?")
    return t.strip()


def _matcher_valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9_]+", value) is not None


def _matcher_compile_pattern(template):
    slot_names = []
    parts = []
    i = 0
    while i < len(template):
        j = template.find("{", i)
        if j < 0:
            parts.append(re.escape(template[i:]))
            break
        parts.append(re.escape(template[i:j]))
        k = template.find("}", j)
        if k < 0:
            return None
        name = template[j + 1:k]
        slot_names.append(name)
        if name == "minutes":
            words = sorted(_MATCHER_NUMBER_WORDS.keys(), key=len, reverse=True)
            alt = "|".join(["\\d+"] + words)
            parts.append("(?P<minutes>(?:" + alt + "))")
        elif name == "message":
            parts.append("(?P<message>.+?)")
        elif name == "camera":
            names = sorted(_MATCHER_CAMERA_NAMES.keys(), key=len, reverse=True)
            parts.append("(?P<camera>(?:" + "|".join(re.escape(n) for n in names) + "))")
        elif name == "time":
            parts.append("(?P<time>(?:\\d{1,2}(?::\\d{2})?\\s*[aApP][mM]|\\d{1,2}:\\d{2}))")
        else:
            return None
        i = k + 1
    try:
        return re.compile("^" + "".join(parts) + "$"), slot_names
    except Exception:
        return None


def _matcher_parse_minutes(raw):
    raw = (raw or "").strip().lower()
    if raw == "a":
        return 1
    if raw.isdigit():
        n = int(raw)
    elif raw in _MATCHER_NUMBER_WORDS:
        n = _MATCHER_NUMBER_WORDS[raw]
    else:
        return None
    if 1 <= n <= 1440:
        return n
    return None


def _matcher_parse_time(raw):
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*([aApP][mM])?", (raw or "").strip())
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    ampm = m.group(3)
    if minute > 59:
        return None
    if ampm:
        if hour < 1 or hour > 12:
            return None
        if ampm.lower() == "am":
            hour = hour % 12
        else:
            hour = hour % 12 + 12
    else:
        if hour > 23:
            return None
    return "%02d:%02d:00" % (hour, minute)


def _matcher_duration(minutes):
    return "%d:%02d:00" % (minutes // 60, minutes % 60)


def _matcher_human_duration(hms):
    try:
        parts = hms.split(":")
        h = int(parts[0])
        m = int(parts[1])
        s = int(parts[2]) if len(parts) > 2 else 0
    except Exception:
        return hms
    bits = []
    if h:
        bits.append("%d hour%s" % (h, "s" if h != 1 else ""))
    if m:
        bits.append("%d minute%s" % (m, "s" if m != 1 else ""))
    if s:
        bits.append("%d second%s" % (s, "s" if s != 1 else ""))
    if not bits:
        return "0 seconds"
    if len(bits) == 1:
        return bits[0]
    return ", ".join(bits[:-1]) + " and " + bits[-1]


def _matcher_format_time(hms):
    m = re.fullmatch(r"(\d{2}):(\d{2})(?::\d{2})?", (hms or "").strip())
    if not m:
        return hms
    hour = int(m.group(1))
    minute = int(m.group(2))
    if hour == 0:
        return "12:%02d AM" % minute
    if hour < 12:
        return "%d:%02d AM" % (hour, minute)
    if hour == 12:
        return "12:%02d PM" % minute
    return "%d:%02d PM" % (hour - 12, minute)


def _matcher_format_value(value):
    if isinstance(value, str):
        low = value.lower()
        if low in _MATCHER_CONDITION_WORDS:
            return _MATCHER_CONDITION_WORDS[low]
        if re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", value.strip()):
            return _matcher_format_time(value.strip())
    return value


def load_matcher_rules():
    try:
        mtime = MATCHER_RULES_PATH.stat().st_mtime
    except Exception:
        return None
    with _MATCHER_LOCK:
        cached = _MATCHER_CACHE["data"]
        if cached is not None and _MATCHER_CACHE["mtime"] == mtime:
            return cached
        try:
            with open(MATCHER_RULES_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:
            log.info("matcher rules unreadable: %s", type(exc).__name__)
            return None
        index = {}
        patterns = []
        for cmd in (raw.get("commands") or []):
            if not isinstance(cmd, dict):
                continue
            cid = cmd.get("id")
            kind = cmd.get("kind")
            if not _matcher_valid_id(cid) or kind not in ("do", "ask"):
                continue
            if kind == "do":
                calls = cmd.get("calls") or []
                if not calls:
                    continue
                ok = True
                for call in calls:
                    if not isinstance(call, dict):
                        ok = False
                        break
                    if call.get("domain") not in _MATCHER_DO_DOMAINS:
                        ok = False
                        break
                    if not isinstance(call.get("service"), str) or not call.get("service"):
                        ok = False
                        break
                if not ok:
                    continue
            else:
                if not isinstance(cmd.get("answer"), str) or not cmd.get("answer"):
                    continue
                if not cmd.get("entity_id") and not cmd.get("entities") and cmd.get("source") != "clock":
                    continue
            for p in (cmd.get("phrases") or []):
                n = matcher_norm(p)
                if n and n not in index:
                    index[n] = cmd
                elif n:
                    log.info("matcher duplicate phrase skipped: %s", n)
            for template in (cmd.get("patterns") or []):
                compiled = _matcher_compile_pattern(matcher_norm(template))
                if compiled is not None:
                    patterns.append((compiled[0], cmd))
        data = {"index": index, "patterns": patterns}
        _MATCHER_CACHE["mtime"] = mtime
        _MATCHER_CACHE["data"] = data
        return data


def match_command(text, rules):
    norm = matcher_norm(text)
    if not norm:
        return None, {}
    if norm in STOP_PHRASES or norm in TIMER_CANCEL_PHRASES:
        return None, {}
    if norm.startswith(("ask muse", "tell muse")):
        return None, {}
    cmd = rules["index"].get(norm)
    if cmd is not None:
        return cmd, {}
    for regex, pcmd in rules["patterns"]:
        m = regex.match(norm)
        if not m:
            continue
        slots = {}
        good = True
        for name, val in m.groupdict().items():
            if name == "minutes":
                n = _matcher_parse_minutes(val)
                if n is None:
                    good = False
                    break
                slots["minutes"] = n
                slots["duration"] = _matcher_duration(n)
            elif name == "time":
                t = _matcher_parse_time(val)
                if t is None:
                    good = False
                    break
                slots["time"] = t
            elif name == "camera":
                cid = _MATCHER_CAMERA_NAMES.get(val)
                if cid is None:
                    good = False
                    break
                slots["camera"] = cid
            elif name == "message":
                msg = (val or "").strip()
                if not msg or len(msg) > 200:
                    good = False
                    break
                slots["message"] = msg
            else:
                slots[name] = val
        if good:
            return pcmd, slots
    return None, {}


def _matcher_resolve(value, slots):
    if not isinstance(value, str):
        return value
    out = value
    for name, val in slots.items():
        out = out.replace("{" + name + "}", str(val))
    if "{" in out and "}" in out:
        return None
    return out


def _matcher_entity_available(entity_id, ha_token):
    if not entity_id:
        return False
    st, data = ha_get_state(entity_id, ha_token)
    if st != 200 or not isinstance(data, dict):
        return False
    return data.get("state") not in ("unavailable", "unknown", "", None)


def _matcher_render_answer(cmd, slots, ha_token):
    template = cmd.get("answer") or ""
    if cmd.get("source") == "clock":
        now = time.localtime()
        vals = {
            "clock_time": time.strftime("%I:%M %p", now).lstrip("0"),
            "clock_date": "%s, %s %d, %d" % (time.strftime("%A", now), time.strftime("%B", now), now.tm_mday, now.tm_year),
            "clock_weekday": time.strftime("%A", now),
        }
        try:
            return template.format(**vals)
        except Exception:
            return None
    entities = {}
    if cmd.get("entity_id"):
        entities["primary"] = cmd.get("entity_id")
    for key, eid in (cmd.get("entities") or {}).items():
        entities[key] = eid
    states = {}
    for key, eid in entities.items():
        resolved = _matcher_resolve(eid, slots)
        if resolved is None:
            return None
        st, data = ha_get_state(resolved, ha_token)
        if st != 200 or not isinstance(data, dict):
            return None
        if data.get("state") in ("unavailable", "unknown", "", None):
            return None
        states[key] = data
    if "primary" not in states and states:
        states["primary"] = states[sorted(states.keys())[0]]

    def repl(m):
        inner = m.group(1)
        if ":" in inner:
            key, attr = inner.split(":", 1)
            data = states.get(key)
            if data is None:
                raise KeyError(inner)
            if attr == "remaining":
                if data.get("state") != "active":
                    return ""
                rem = (data.get("attributes") or {}).get("remaining")
                if not rem:
                    return ""
                return ", " + _matcher_human_duration(rem) + " left"
            val = (data.get("attributes") or {}).get(attr)
            if val is None:
                raise KeyError(inner)
            return str(_matcher_format_value(val))
        if inner == "state":
            data = states.get("primary")
            if data is None:
                raise KeyError(inner)
            return str(_matcher_format_value(data.get("state")))
        if inner.startswith("attr:"):
            data = states.get("primary")
            if data is None:
                raise KeyError(inner)
            val = (data.get("attributes") or {}).get(inner[5:])
            if val is None:
                raise KeyError(inner)
            return str(_matcher_format_value(val))
        data = states.get(inner)
        if data is None:
            raise KeyError(inner)
        return str(_matcher_format_value(data.get("state")))

    try:
        return re.sub(r"\{([^{}]+)\}", repl, template)
    except KeyError:
        return None


def try_matcher(lowered, ha_token):
    try:
        rules = load_matcher_rules()
        if rules is None:
            return None
        cmd, slots = match_command(lowered, rules)
        if cmd is None:
            return None
        if cmd["kind"] == "ask":
            text = _matcher_render_answer(cmd, slots, ha_token)
            if text is None:
                return None
            return {"outcome": "execute", "command_id": cmd["id"], "kind": "ask", "reply_text": text, "actions": []}
        calls = []
        for call in (cmd.get("calls") or []):
            eid = _matcher_resolve(call.get("entity_id", ""), slots)
            if eid is None:
                return None
            data = {}
            for k, v in (call.get("data") or {}).items():
                rv = _matcher_resolve(v, slots)
                if rv is None:
                    return None
                data[k] = rv
            if call.get("entity_id"):
                data["entity_id"] = eid
            calls.append({"domain": call["domain"], "service": call["service"], "entity_id": eid, "data": data})
        seen = []
        for c in calls:
            if c["entity_id"] and c["entity_id"] not in seen:
                seen.append(c["entity_id"])
        for eid in seen:
            if not _matcher_entity_available(eid, ha_token):
                return None
        actions = []
        for c in calls:
            st, _b = ha_service(c["domain"], c["service"], ha_token, c["data"])
            actions.append({"type": "ha_call_service", "domain": c["domain"], "service": c["service"], "entity_id": c["entity_id"], "http_status": st})
        ok = [a for a in actions if a.get("http_status") in (200, 201)]
        if actions and len(ok) == len(actions):
            return {"outcome": "execute", "command_id": cmd["id"], "kind": "do", "reply_text": ACK_SENTINEL, "actions": actions}
        if not ok:
            return None
        failed = ", ".join(a.get("entity_id") or (a.get("domain") + "." + a.get("service")) for a in actions if a.get("http_status") not in (200, 201))
        return {"outcome": "execute", "command_id": cmd["id"], "kind": "do", "reply_text": "I did part of that, but could not finish: " + failed + ".", "actions": actions}
    except Exception as exc:
        log.info("matcher error: %s", type(exc).__name__)
        return None


def test_matcher(text, ha_token):
    norm = matcher_norm(text)
    if not norm:
        return {"matched": False, "source": "empty"}
    if norm in STOP_PHRASES:
        return {"matched": True, "source": "keyword", "command_id": "stop", "kind": "do", "would": "Stop all playback, silently."}
    if norm in TIMER_CANCEL_PHRASES:
        return {"matched": True, "source": "keyword", "command_id": "timer_cancel", "kind": "do", "would": "Cancel the office timer."}
    if norm.startswith(("ask muse", "tell muse")):
        return {"matched": True, "source": "keyword", "command_id": "ask_muse", "kind": "ask", "would": "Relay to Muse (Asking Muse.)."}
    rules = load_matcher_rules()
    if rules is None:
        return {"matched": False, "source": "matcher-unavailable"}
    cmd, slots = match_command(norm, rules)
    if cmd is None:
        return {"matched": False, "source": "fallback", "would": "Decide, then Mercury 3."}
    if cmd["kind"] == "ask":
        text_out = _matcher_render_answer(cmd, slots, ha_token)
        if text_out is None:
            return {"matched": True, "source": "matcher", "command_id": cmd["id"], "kind": "ask", "would": None, "note": "Entity unavailable or unanswered; would fall back to Mercury 3."}
        return {"matched": True, "source": "matcher", "command_id": cmd["id"], "kind": "ask", "would": text_out}
    calls = []
    for call in (cmd.get("calls") or []):
        eid = _matcher_resolve(call.get("entity_id", ""), slots)
        data = {}
        for k, v in (call.get("data") or {}).items():
            data[k] = _matcher_resolve(v, slots)
        if call.get("entity_id"):
            data["entity_id"] = eid
        calls.append({"domain": call["domain"], "service": call["service"], "entity_id": eid, "data": data})
    avail = {}
    for c in calls:
        if c["entity_id"] and c["entity_id"] not in avail:
            if not ha_token:
                avail[c["entity_id"]] = None
            else:
                avail[c["entity_id"]] = _matcher_entity_available(c["entity_id"], ha_token)
    return {"matched": True, "source": "matcher", "command_id": cmd["id"], "kind": "do", "calls": calls, "target_available": avail, "would": "ding"}



class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                data = UI_PATH.read_bytes()
            except Exception:
                data = b"<h1>Mercury Engine</h1><p>UI file missing.</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/health":
            self.send_json({"ok": True, "service": "mercury-engine"})
        elif self.path == "/api/status":
            cfg = load_config()
            vf = voice_file()
            self.send_json({
                "ok": True,
                "inception_key_set": bool(cfg["inception_key"]),
                "ha_token_set": bool(cfg["ha_token"]),
                "google_api_key_set": bool(cfg["google_api_key"]),
                "groq_api_key_set": bool(cfg["groq_api_key"]),
                "voice_choice": cfg["voice_choice"],
                "google_voice_name": cfg["google_voice_name"],
                "whisper_ready": (BASE_DIR / "whisper_ready.txt").exists(),
                "piper_ready": (BASE_DIR / "piper_ready.txt").exists(),
                "voice": vf.name if vf else None,
            })
        elif self.path.startswith("/clips/"):
            name = self.path[len("/clips/"):]
            if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.mp3", name):
                self.send_json({"error": "not found"}, status=404)
            else:
                clip_path = CLIPS_DIR / name
                try:
                    data = clip_path.read_bytes()
                except Exception:
                    self.send_json({"error": "not found"}, status=404)
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
        else:
            self.send_json({"error": "not found"}, status=404)

    def do_POST(self):
        cfg = load_config()
        if self.path == "/api/config":
            patch = read_json_body(self)
            result = save_config(patch)
            self.send_json({"ok": True, **result})
        elif self.path == "/api/transcribe":
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 25 * 1024 * 1024:
                self.send_json({"error": "bad audio upload"}, status=400)
                return
            audio = self.rfile.read(length)
            tmp = BASE_DIR / "upload.webm"
            tmp.write_bytes(audio)
            try:
                _ws_text, _ws_engine, _ws_reason = transcribe_with_fallback(str(tmp))
                self.send_json({"text": _ws_text, "asr_engine": _ws_engine, "asr_fallback_reason": _ws_reason})
            except Exception as exc:
                log.info("transcribe failed: %s", type(exc).__name__)
                self.send_json({"error": "transcription failed"}, status=500)
        elif self.path == "/api/speak-clip":
            body = read_json_body(self)
            text = (body.get("text") or "").strip()[:1000]
            if not text:
                self.send_json({"error": "empty text"}, status=400)
                return
            try:
                CLIPS_DIR.mkdir(exist_ok=True)
                now = time.time()
                for old in CLIPS_DIR.glob("*.mp3"):
                    try:
                        if now - old.stat().st_mtime > 600:
                            old.unlink()
                    except Exception:
                        pass
            except Exception:
                pass
            if cfg["voice_choice"] != "google" or not cfg["google_api_key"]:
                self.send_json({"error": "google tts not configured"}, status=503)
                return
            data = google_tts(text, cfg["google_api_key"], cfg.get("google_voice_name", DEFAULT_GOOGLE_VOICE))
            if not data:
                self.send_json({"error": "google tts failed"}, status=502)
                return
            clip_id = str(uuid.uuid4())
            clip_path = CLIPS_DIR / (clip_id + ".mp3")
            try:
                clip_path.write_bytes(data)
            except Exception:
                self.send_json({"error": "clip write failed"}, status=500)
                return
            self.send_json({"url": "http://YOUR_THINCLIENT_IP:8896/clips/" + clip_id + ".mp3"})
        elif self.path == "/api/tts":
            body = read_json_body(self)
            text = (body.get("text") or "")[:1000]
            if not text:
                self.send_json({"error": "empty text"}, status=400)
                return
            if cfg["voice_choice"] == "google" and cfg["google_api_key"]:
                data = google_tts(text, cfg["google_api_key"], cfg.get("google_voice_name", DEFAULT_GOOGLE_VOICE))
                if data:
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                log.info("google tts unavailable, falling back to piper")
            vf = voice_file()
            if vf is None or not VENV_PIPER.exists():
                self.send_json({"error": "piper not ready"}, status=503)
                return
            out_wav = BASE_DIR / "reply.wav"
            try:
                proc = subprocess.run(
                    [str(VENV_PIPER), "--model", str(vf),
                     "--output_file", str(out_wav)],
                    input=text.encode("utf-8"),
                    capture_output=True, timeout=60)
                if not out_wav.exists() or out_wav.stat().st_size == 0:
                    raise RuntimeError("piper produced no audio")
                data = out_wav.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                log.info("tts failed: %s", type(exc).__name__)
                self.send_json({"error": "tts failed"}, status=500)
        elif self.path == "/api/ask":
            body = read_json_body(self)
            text = (body.get("text") or "").strip()
            if not text:
                self.send_json({"error": "empty text"}, status=400)
                return
            if not cfg["inception_key"] or not cfg["ha_token"]:
                self.send_json({
                    "error": "keys_not_configured",
                    "inception_key_set": bool(cfg["inception_key"]),
                    "ha_token_set": bool(cfg["ha_token"])}, status=400)
                return
            lowered = text.lower().strip().rstrip(".!")
            if lowered in STOP_PHRASES:
                route = {"route": "stop", "confidence": 1.0,
                         "source": "keyword"}
            else:
                route = decide_route(text, cfg["inception_key"])
            if is_fragment_transcript(lowered):
                reply = mercury3_answer(text, cfg["inception_key"],
                                        cfg["ha_token"], clarify_only=True)
                self.send_json({"action": "answer",
                                "route": {"route": "mercury3",
                                          "confidence": 0.0,
                                          "source": "fragment-fail-open"},
                                "reply_text": reply})
                return
            if route["route"] == "stop":
                _tc, _tc_actions = try_timer_cancel(lowered,
                                                    cfg["ha_token"])
                if _tc:
                    self.send_json({"action": "timer_cancelled",
                                    "reply_text": "Timer cancelled.",
                                    "route": route,
                                    "actions": _tc_actions})
                    return
                _ask_stop = stop_ha_media(cfg["ha_token"])
                _ask_st = _ask_stop[0]["http_status"] if _ask_stop else None
                log.info("stop action -> HA HTTP %s", _ask_st)
                self.send_json({"action": "stop", "reply_text": "",
                                "route": route, "ha_status": _ask_st,
                                "actions": _ask_stop})
                return
            if route["route"] == "ask_muse" or lowered.startswith(
                    ("ask muse", "tell muse")):
                marker = ask_muse_marker(text, cfg["ha_token"])
                self.send_json({
                    "action": "ask_muse", "route": route,
                    "reply_text": "Asking Muse.",
                    "marker": {"marker_sent": marker["marker_sent"],
                               "http_status": marker["http_status"]}})
                return
            reply = mercury3_answer(text, cfg["inception_key"],
                                    cfg["ha_token"])
            self.send_json({"action": "answer", "route": route,
                            "reply_text": reply})
        elif self.path == "/api/turn":
            body = read_json_body(self)
            audio_b64 = body.get("audio_b64") or ""
            end_reason = body.get("end_reason") or ""
            log.info("turn device=%s end_reason=%s",
                     body.get("device"), end_reason)
            _t0 = time.monotonic()
            _t_transcribe = None
            _t_decide = None
            _t_mercury = None
            _t_tts = None
            MERCURY_LAST["data"] = None
            _ev_base = {
                "schema_version": 1,
                "source": "mercury-engine",
                "ts": time.time(),
                "device": body.get("device"),
                "session_id": body.get("session_id"),
                "mode": body.get("mode") or "live",
                "end_reason": end_reason,
            }
            try:
                raw = base64.b64decode(audio_b64, validate=True)
            except Exception:
                self.send_json({"error": "bad audio_b64"}, status=400)
                return
            if not raw:
                self.send_json({"error": "empty audio"}, status=400)
                return
            if not VENV_PY.exists() or not TRANSCRIBE_SCRIPT.exists():
                self.send_json({"error": "whisper not installed"}, status=503)
                return
            with tempfile.TemporaryDirectory() as td:
                wav_path = Path(td) / "turn.wav"
                if raw[:4] == b"RIFF":
                    wav_path.write_bytes(raw)
                else:
                    # Raw PCM16 from the Dot: wrap as 16 kHz mono WAV.
                    wav_path.write_bytes(_wav_header_pcm16(len(raw)) + raw)
                try:
                    _t_transcribe = time.monotonic()
                    transcript, _asr_engine, _asr_reason = transcribe_with_fallback(str(wav_path))
                    _ev_base["asr"] = {"engine": _asr_engine, "fallback_reason": _asr_reason}
                except Exception as exc:
                    try:
                        _ev_base["asr"] = {"engine": "error", "fallback_reason": "transcription_failed"}
                        emit_voice_event({**_ev_base, "transcript": None, "audio_size_bytes": len(raw), "audio_format": "wav" if raw[:4] == b"RIFF" else "pcm16", "error": {"type": "transcription_failed"}, "latencies": {"transcribe_ms": _dur(_t_transcribe, _t0), "total_ms": round((time.monotonic() - _t0) * 1000, 1)}})
                    except Exception:
                        pass
                    log.info("turn transcribe failed: %s",
                             type(exc).__name__)
                    self.send_json({"error": "transcription failed"},
                                   status=500)
                    return
            engine = selected_engine(cfg)
            lowered = transcript.lower().strip().rstrip(".!")
            mode = body.get("mode") or "live"
            if mode not in ("live", "shadow"):
                self.send_json({"error": "bad mode"}, status=400)
                return
            if mode == "shadow":
                # Shadow: transcribe + Decide ONLY. No keyword
                # short-circuit (the fork logs Decide's own
                # routing, including for stop phrases), no
                # Mercury 3, no HA action, no TTS. stop stays
                # false because shadow takes no action at all.
                if not transcript.strip() or end_reason == "no_speech":
                    try:
                        emit_voice_event({**_ev_base, "transcript": transcript, "audio_size_bytes": len(raw), "audio_format": "wav" if raw[:4] == b"RIFF" else "pcm16", "decide": None, "decide_reason": "no_speech_or_empty", "mercury": None, "actions": [], "tts": None, "stop": False, "latencies": {"transcribe_ms": _dur(_t_transcribe, _t0), "total_ms": round((time.monotonic() - _t0) * 1000, 1)}})
                    except Exception:
                        pass
                    log.info("turn shadow no-reply end_reason=%s",
                             end_reason)
                    self.send_json({"transcript": transcript,
                                    "decide_verdict": "no_reply",
                                    "decide_confidence": 0.0,
                                    "actions": [], "engine": None,
                                    "stop": False, "audio_b64": "",
                                    "audio_format": "wav48k"})
                    return
                if not cfg["inception_key"]:
                    self.send_json({"error": "keys_not_configured"},
                                   status=400)
                    return
                _t_decide = time.monotonic()
                route = decide_route(transcript, cfg["inception_key"])
                try:
                    emit_voice_event({**_ev_base, "transcript": transcript, "audio_size_bytes": len(raw), "audio_format": "wav" if raw[:4] == b"RIFF" else "pcm16", "decide": {"source": route["source"], "verdict": route["route"], "confidence": route["confidence"], "probabilities": route.get("probabilities", {}), "stop_confidence": route.get("stop_confidence"), "request": route.get("request")}, "mercury": None, "actions": [], "tts": None, "stop": False, "latencies": {"transcribe_ms": _dur(_t_transcribe, _t0), "decide_ms": _dur(_t_decide, _t_transcribe), "total_ms": round((time.monotonic() - _t0) * 1000, 1)}})
                except Exception:
                    pass
                log.info("turn shadow verdict=%s", route["route"])
                self.send_json({"transcript": transcript,
                                "decide_verdict": route["route"],
                                "decide_confidence": route["confidence"],
                                "actions": [], "engine": None,
                                "stop": False, "audio_b64": "",
                                "audio_format": "wav48k"})
                return
            if lowered in TIMER_CANCEL_PHRASES:
                _tc, _tc_actions = try_timer_cancel(lowered,
                                                    cfg["ha_token"])
                if _tc:
                    try:
                        emit_voice_event({**_ev_base, "transcript": transcript, "audio_size_bytes": len(raw), "audio_format": "wav" if raw[:4] == b"RIFF" else "pcm16", "decide": {"source": "timer_cancel", "verdict": "stop", "confidence": 1.0}, "mercury": None, "actions": _tc_actions, "tts": None, "stop": True, "latencies": {"transcribe_ms": _dur(_t_transcribe, _t0), "total_ms": round((time.monotonic() - _t0) * 1000, 1)}})
                    except Exception:
                        pass
                    resume_media_now(cfg["ha_token"], "timer-cancel")
                    log.info("turn timer cancelled (keyword)")
                    self.send_json({"transcript": transcript,
                                    "decide_verdict": "stop",
                                    "decide_confidence": 1.0,
                                    "actions": _tc_actions, "engine": engine,
                                    "stop": True, "audio_b64": "",
                                    "audio_format": "wav48k"})
                    return
            if lowered in STOP_PHRASES:
                # Exact-stop pre-check FIRST: no LLM. The fork stops
                # its own reply-audio channel on stop=true, but HA-side
                # playback (pink/white noise on the Dot speaker) is a
                # separate stream: stop it here too (fix-forward Oct 24:
                # keyword stops previously left HA noise playing).
                _stop_actions = stop_ha_media(cfg["ha_token"])
                try:
                    emit_voice_event({**_ev_base, "transcript": transcript, "audio_size_bytes": len(raw), "audio_format": "wav" if raw[:4] == b"RIFF" else "pcm16", "decide": {"source": "keyword", "verdict": "stop", "confidence": 1.0}, "mercury": None, "actions": _stop_actions, "tts": None, "stop": True, "latencies": {"transcribe_ms": _dur(_t_transcribe, _t0), "total_ms": round((time.monotonic() - _t0) * 1000, 1)}})
                except Exception:
                    pass
                cancel_paused_media()
                log.info("turn stop-phrase (keyword)")
                self.send_json({"transcript": transcript,
                                "decide_verdict": "stop",
                                "decide_confidence": 1.0,
                                "actions": _stop_actions, "engine": engine,
                                "stop": True, "audio_b64": "",
                                "audio_format": "wav48k"})
                return
            if not transcript.strip() or end_reason == "no_speech":
                try:
                    emit_voice_event({**_ev_base, "transcript": transcript, "audio_size_bytes": len(raw), "audio_format": "wav" if raw[:4] == b"RIFF" else "pcm16", "decide": None, "decide_reason": "no_speech_or_empty", "mercury": None, "actions": [], "tts": None, "stop": False, "latencies": {"transcribe_ms": _dur(_t_transcribe, _t0), "total_ms": round((time.monotonic() - _t0) * 1000, 1)}})
                except Exception:
                    pass
                resume_media_now(cfg["ha_token"], "no-speech")
                log.info("turn no-reply end_reason=%s", end_reason)
                self.send_json({"transcript": transcript,
                                "decide_verdict": "no_reply",
                                "decide_confidence": 0.0,
                                "actions": [], "engine": engine,
                                "stop": False, "audio_b64": "",
                                "audio_format": "wav48k"})
                return
            if not cfg["inception_key"] or not cfg["ha_token"]:
                self.send_json({"error": "keys_not_configured"}, status=400)
                return
            _matched = try_matcher(lowered, cfg["ha_token"])
            if _matched is not None and _matched["outcome"] == "execute":
                reply_text = _matched["reply_text"]
                actions = _matched["actions"]
                route = {"source": "matcher", "route": "matcher", "confidence": 1.0}
                verdict = "matcher"
                conf = 1.0
                _clarify_only = False
                _t_decide = time.monotonic()
                MERCURY_LAST["data"] = None
                _ev_base["matcher"] = {"command_id": _matched["command_id"], "kind": _matched["kind"]}
            else:
                _clarify_only = False
                if is_fragment_transcript(lowered):
                    _t_decide = time.monotonic()
                    route = {"route": "mercury3", "confidence": 0.0,
                             "source": "fragment-fail-open"}
                    verdict = "mercury3"
                    conf = 0.0
                    actions = []
                    _clarify_only = True
                    pause_media_for_session(body.get("session_id") or "fragment",
                                            cfg["ha_token"])
                else:
                    _t_decide = time.monotonic()
                    route = decide_route(transcript, cfg["inception_key"])
                    verdict = route["route"]
                    conf = route["confidence"]
                    actions = []
                if verdict == "stop":
                    _tc, _tc_actions = try_timer_cancel(lowered,
                                                        cfg["ha_token"])
                    if _tc:
                        actions.extend(_tc_actions)
                    else:
                        # Decide-inferred stop: stop ALL HA-side playback.
                        actions.extend(stop_ha_media(cfg["ha_token"]))
                    try:
                        emit_voice_event({**_ev_base, "transcript": transcript, "audio_size_bytes": len(raw), "audio_format": "wav" if raw[:4] == b"RIFF" else "pcm16", "decide": {"source": route["source"], "verdict": route["route"], "confidence": route["confidence"], "probabilities": route.get("probabilities", {}), "stop_confidence": route.get("stop_confidence"), "request": route.get("request")}, "mercury": None, "actions": actions, "tts": None, "stop": True, "latencies": {"transcribe_ms": _dur(_t_transcribe, _t0), "decide_ms": _dur(_t_decide, _t_transcribe), "total_ms": round((time.monotonic() - _t0) * 1000, 1)}})
                    except Exception:
                        pass
                    if _tc:
                        resume_media_now(cfg["ha_token"], "timer-cancel")
                    else:
                        cancel_paused_media()
                    self.send_json({"transcript": transcript,
                                    "decide_verdict": verdict,
                                    "decide_confidence": conf,
                                    "actions": actions, "engine": engine,
                                    "stop": True, "audio_b64": "",
                                    "audio_format": "wav48k"})
                    return
                if verdict == "ask_muse" or lowered.startswith(
                        ("ask muse", "tell muse")):
                    marker = ask_muse_marker(transcript, cfg["ha_token"])
                    actions.append({"type": "ask_muse_marker",
                                    "marker_sent": marker["marker_sent"],
                                    "http_status": marker["http_status"]})
                    reply_text = "Asking Muse."
                else:
                    reply_text = mercury3_answer(
                        transcript, cfg["inception_key"], cfg["ha_token"],
                        actions=actions, clarify_only=_clarify_only)
                    _guarded = guard_done_claim(reply_text, actions)
                    if _guarded != reply_text:
                        log.info("turn done-claim guarded (no successful service call)")
                        reply_text = _guarded
                        if MERCURY_LAST["data"] is not None:
                            MERCURY_LAST["data"]["reply_text"] = reply_text
            _t_mercury = time.monotonic()
            _ack_ok = (reply_text.strip() == ACK_SENTINEL and any(
                a.get("type") == "ha_call_service"
                and a.get("http_status") in (200, 201)
                for a in actions))
            if reply_text.strip() == ACK_SENTINEL and not _ack_ok:
                # Sentinel without a successful service call: never
                # speak it; fall back to an honest failure message.
                log.info("turn ack without successful service call; "
                         "replacing with failure message")
                reply_text = ("I tried that but did not get a final "
                              "confirmation from the device, so I cannot "
                              "confirm it worked.")
                if MERCURY_LAST["data"] is not None:
                    MERCURY_LAST["data"]["reply_text"] = reply_text
            _ding = load_ding_wav() if _ack_ok else None
            if _ding is not None:
                log.info("turn ack-ding (simple command succeeded)")
                eng = "ding"
                wav48 = _ding
                _t_tts = time.monotonic()
            else:
                if _ack_ok:
                    # Ding file missing: speak a short confirmation
                    # rather than the sentinel or silence.
                    log.info("turn ack-ding file missing; speaking Done.")
                    reply_text = "Done."
                eng, raw_audio = synthesize_reply(reply_text, cfg)
                if raw_audio is None:
                    self.send_json({"error": "tts failed"}, status=500)
                    return
                _t_tts = time.monotonic()
                wav48 = transcode_wav48k(raw_audio)
                if wav48 is None:
                    self.send_json({"error": "transcode failed"}, status=500)
                    return
            _reply_dur = round(len(wav48) / 96000, 3)
            _followup = bool(_clarify_only or reply_text.strip().endswith("?"))
            if _turn_took_over_media(actions):
                cancel_paused_media()
            elif not _followup:
                schedule_media_resume(cfg["ha_token"], _reply_dur + 1.5)
            try:
                _vf = voice_file()
                _voice_name = ("ding" if eng == "ding" else
                               (cfg.get("google_voice_name") if eng == "wavenet" else (_vf.name if _vf is not None else "piper")))
            except Exception:
                _voice_name = eng
            try:
                emit_voice_event({**_ev_base, "transcript": transcript, "audio_size_bytes": len(raw), "audio_format": "wav" if raw[:4] == b"RIFF" else "pcm16", "decide": {"source": route["source"], "verdict": route["route"], "confidence": route["confidence"], "probabilities": route.get("probabilities", {}), "stop_confidence": route.get("stop_confidence"), "request": route.get("request")}, "mercury": MERCURY_LAST["data"], "reply_text": reply_text, "actions": actions, "tts": {"engine": eng, "voice": _voice_name, "text": reply_text, "audio_bytes": len(wav48), "audio_format": "wav48k", "duration_s": round(len(wav48) / 96000, 3)}, "stop": False, "latencies": {"transcribe_ms": _dur(_t_transcribe, _t0), "decide_ms": _dur(_t_decide, _t_transcribe), "mercury_ms": _dur(_t_mercury, _t_decide), "tts_ms": _dur(_t_tts, _t_mercury), "total_ms": round((time.monotonic() - _t0) * 1000, 1)}})
            except Exception:
                pass
            self.send_json({"transcript": transcript,
                            "decide_verdict": verdict,
                            "decide_confidence": conf,
                            "actions": actions, "engine": eng,
                            "stop": False, "followup": _followup,
                            "audio_b64": base64.b64encode(wav48).decode(
                                "ascii"),
                            "audio_format": "wav48k"})
        elif self.path == "/api/matcher/test":
            body = read_json_body(self)
            text = (body.get("text") or "").strip()
            if not text:
                self.send_json({"error": "empty text"}, status=400)
            else:
                self.send_json(test_matcher(text, cfg["ha_token"]))
        elif self.path == "/api/stop":
            ha_status = None
            if cfg["ha_token"]:
                _stop_acts = stop_ha_media(cfg["ha_token"])
                cancel_paused_media()
                ha_status = _stop_acts[0]["http_status"] if _stop_acts else None
            self.send_json({"stopped": True, "ha_status": ha_status})
        elif self.path == "/api/session_start":
            body = read_json_body(self)
            if not cfg["ha_token"]:
                self.send_json({"error": "keys_not_configured"}, status=400)
                return
            result = pause_media_for_session(
                body.get("session_id") or "", cfg["ha_token"])
            self.send_json({"paused": result["paused"],
                            "player_state_before": result["state_before"],
                            "auto_resume_s": _MEDIA_PAUSE_AUTO_RESUME_S})
        elif self.path == "/api/session_end":
            _body = read_json_body(self)
            resumed = resume_media_now(
                cfg["ha_token"], "session-end") if cfg["ha_token"] else False
            self.send_json({"resumed": resumed})
        elif self.path == "/api/voice-check":
            if not cfg["inception_key"]:
                self.send_json({"error": "keys_not_configured"}, status=400)
                return
            status, _body = post_json(
                INCEPTION_BASE + "/chat/completions", cfg["inception_key"],
                {"model": "mercury-voice",
                 "messages": [{"role": "user", "content": "Say hi."}],
                 "max_tokens": 1}, timeout=30)
            self.send_json({"available": status == 200,
                            "http_status": status})
        else:
            self.send_json({"error": "not found"}, status=404)


def main():
    port = int(os.environ.get("MERCURY_ENGINE_PORT", "8896"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("mercury-engine listening on 0.0.0.0:%s", port)
    print(f"Mercury Engine listening on http://localhost:{port}/", flush=True)
    tls_port = int(os.environ.get("MERCURY_ENGINE_TLS_PORT", "8444"))
    tls_cert = Path(os.environ.get("MERCURY_ENGINE_TLS_CERT", str(BASE_DIR / "tls" / "cert.pem")))
    tls_key = Path(os.environ.get("MERCURY_ENGINE_TLS_KEY", str(BASE_DIR / "tls" / "key.pem")))
    if tls_cert.is_file() and tls_key.is_file():
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(tls_cert), str(tls_key))
            tls_server = ThreadingHTTPServer(("0.0.0.0", tls_port), Handler)
            tls_server.socket = ctx.wrap_socket(tls_server.socket, server_side=True)
            tls_thread = threading.Thread(target=tls_server.serve_forever, daemon=True)
            tls_thread.start()
            log.info("mercury-engine TLS listening on 0.0.0.0:%s", tls_port)
            print(f"Mercury Engine TLS listening on https://localhost:{tls_port}/", flush=True)
        except Exception as exc:
            log.warning("mercury-engine TLS disabled: %s", exc)
    else:
        log.warning("mercury-engine TLS disabled: cert or key missing")
    server.serve_forever()


if __name__ == "__main__":
    main()
