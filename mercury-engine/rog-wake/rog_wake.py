#!/usr/bin/env python3
"""ROG wake-word listener: "Hey Jarvis" -> Mercury POC turn -> spoken reply.

Listens on a desk mic with openWakeWord (built-in hey_jarvis model),
captures the command spoken after the wake word, POSTs it to the Mercury
POC /api/turn as a 16 kHz WAV, and plays the 48 kHz WAV reply back.
Same brain as the Office Dot (Mercury 3 + HA actions + ding + stop),
no Dot involved.

Modes:
  python rog_wake.py                  listen forever (console + log file)
  python rog_wake.py --list-mics      list input devices and exit
  python rog_wake.py --selftest       model loads, mic reads, POC reachable
  python rog_wake.py --test-turn f.wav
                                      POST a WAV as a turn, save reply (no playback)

Config: config.json next to this script (all keys optional, defaults below).
  poc_endpoint: POC base URL (default http://YOUR_THINCLIENT_IP:8896)
  mic_substring: pin a mic by substring of its device name ("" = system default)
  wake_threshold: openWakeWord score that counts as "Hey Jarvis" (default 0.5)
  silence_end_s: quiet time that ends a capture (default 1.0)
  max_capture_s: longest single command (default 15.0)
  no_speech_timeout_s: give up if nothing is said after the wake word (default 5.0)

Notes:
- The listener does NOT call POC /api/session_start or /api/session_end.
  The POC's media-pause flag is a single global, not per-session, so a ROG
  session could resume Dot media in the middle of a Dot turn. If desk-media
  garbling shows up live, that is a POC-side fix, not a ROG-side one.
- While a reply plays, the mic is ignored (frames drained afterwards), so
  the speaker cannot re-trigger the listener.
- POC unreachable never crashes the listener: it logs and keeps listening.
"""

import argparse
import base64
import io
import json
import logging
import os
import queue
import sys
import threading
import time
import uuid
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {
    "poc_endpoint": "http://YOUR_THINCLIENT_IP:8896",
    "mic_substring": "",
    "wake_threshold": 0.5,
    "silence_end_s": 1.0,
    "max_capture_s": 15.0,
    "no_speech_timeout_s": 5.0,
    "device": "rog",
    "log_file": os.path.join(HERE, "rog-wake.log"),
}
SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80 ms at 16 kHz, openWakeWord's native frame
SPEECH_RMS = 0.02     # normalized RMS that counts as speech
SILENCE_RMS = 0.01    # normalized RMS that counts as quiet


def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(HERE, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as exc:  # noqa: BLE001 - config must never kill startup
            print(f"config.json unreadable ({exc}); using defaults", file=sys.stderr)
    return cfg


def setup_logging(log_file):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    return logging.getLogger("rog-wake")


def load_wake_model():
    """Load the built-in hey_jarvis model (downloads on first run).

    openWakeWord 0.6: the model files are NOT in the wheel — they must be
    fetched once with download_models(), and the onnx runtime must be
    selected explicitly (the default tflite path needs tflite-runtime,
    which is not installed on the ROG).
    """
    import openwakeword
    from openwakeword.model import Model

    try:
        openwakeword.utils.download_models(model_names=["hey_jarvis"])
    except TypeError:
        try:
            openwakeword.utils.download_models()
        except Exception:  # noqa: BLE001 - Model() below reports the real error
            pass
    except Exception:  # noqa: BLE001
        pass
    try:
        return Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
    except TypeError:
        try:
            return Model(wakeword_model_paths=["hey_jarvis"])
        except TypeError:
            return Model(wakeword_models=["hey_jarvis"])


def pick_mic(mic_substring):
    import sounddevice as sd

    devices = sd.query_devices()
    if mic_substring:
        needle = mic_substring.lower()
        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0 and needle in dev["name"].lower():
                return i, dev["name"]
        raise SystemExit(f"No input device matches {mic_substring!r} (see --list-mics)")
    default_in = sd.default.device[0]
    if default_in is None:
        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0:
                return i, dev["name"]
        raise SystemExit("No input device found")
    return default_in, devices[default_in]["name"]


def list_mics():
    import sounddevice as sd

    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            print(f"[{i}] {dev['name']} (in={dev['max_input_channels']}, "
                  f"default_rate={dev['default_samplerate']})")
    print(f"default input: {sd.default.device[0]}")


def build_wav(pcm_bytes, rate=SAMPLE_RATE):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm_bytes)
    return buf.getvalue()


def parse_wav(wav_bytes):
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        return w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())


def post_json(url, payload, timeout):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": f"HTTP {exc.code}"}


def get_json(url, timeout=10):
    import urllib.request

    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def play_reply(wav_bytes, log):
    import sounddevice as sd

    rate, channels, pcm = parse_wav(wav_bytes)
    audio = np.frombuffer(pcm, dtype=np.int16)
    if channels == 2:
        audio = audio.reshape(-1, 2)
    log.info("playing reply: %.1fs at %d Hz", len(audio) / rate, rate)
    sd.play(audio, samplerate=rate)
    sd.wait()


def do_turn(cfg, log, pcm_bytes, end_reason):
    """POST one captured command to the POC. Returns the response dict."""
    wav = build_wav(pcm_bytes)
    payload = {
        "session_id": str(uuid.uuid4()),
        "device": cfg["device"],
        "audio_b64": base64.b64encode(wav).decode("ascii"),
        "end_reason": end_reason,
        "mode": "live",
    }
    status, data = post_json(cfg["poc_endpoint"] + "/api/turn", payload, timeout=70)
    if status != 200:
        raise RuntimeError(f"POC /api/turn HTTP {status}: {data}")
    return data


class MicStream:
    """16 kHz mono int16 mic frames into a queue."""

    def __init__(self, device_index):
        import sounddevice as sd

        self.sd = sd
        self.q = queue.Queue()
        self.stream = sd.InputStream(
            device=device_index,
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            callback=self._callback,
        )

    def _callback(self, indata, frames, time_info, status):
        if status:
            pass  # xruns are logged by sounddevice; keep listening
        self.q.put(bytes(indata))

    def __enter__(self):
        self.stream.start()
        return self

    def __exit__(self, *exc):
        self.stream.stop()
        self.stream.close()

    def read_frame(self, timeout=None):
        return self.q.get(timeout=timeout)

    def drain(self):
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return


def rms_norm(frame_bytes):
    audio = np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(audio * audio)))


def listen_loop(cfg, log):
    import sounddevice as sd  # noqa: F401 - import validates install early

    model = load_wake_model()
    log.info("wake model loaded (hey_jarvis, threshold %.2f)", cfg["wake_threshold"])
    mic_index, mic_name = pick_mic(cfg["mic_substring"])
    log.info("mic: [%d] %s", mic_index, mic_name)
    log.info("listening for 'Hey Jarvis'... (Ctrl+C to stop)")

    with MicStream(mic_index) as mic:
        ring = []  # last ~0.5 s, prepended so fast talkers are not clipped
        while True:
            frame = mic.read_frame()
            ring.append(frame)
            if len(ring) > 6:
                ring.pop(0)
            try:
                scores = model.predict(np.frombuffer(frame, dtype=np.int16))
            except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the loop
                log.warning("predict failed: %s", exc)
                continue
            score = float(scores.get("hey_jarvis", 0.0))
            if score < cfg["wake_threshold"]:
                continue
            log.info("wake word detected (score %.3f)", score)
            model.reset()
            handle_command(cfg, log, model, mic, ring)
            ring = []
            mic.drain()
            model.reset()
            log.info("listening for 'Hey Jarvis'...")


def handle_command(cfg, log, model, mic, preroll):
    """Capture one command after the wake word, send it, play the reply."""
    frames = list(preroll)
    speech_seen = False
    last_speech = time.monotonic()
    started = time.monotonic()
    while True:
        try:
            frame = mic.read_frame(timeout=0.2)
        except queue.Empty:
            frame = None
        now = time.monotonic()
        if frame is not None:
            frames.append(frame)
            level = rms_norm(frame)
            if level > SPEECH_RMS:
                speech_seen = True
                last_speech = now
        if speech_seen and (now - last_speech) >= cfg["silence_end_s"]:
            end_reason = "vad_end"
            break
        if not speech_seen and (now - started) >= cfg["no_speech_timeout_s"]:
            log.info("no speech after wake word; ignoring")
            return
        if (now - started) >= cfg["max_capture_s"]:
            end_reason = "vad_end"
            break
    pcm = b"".join(frames)
    log.info("captured %.1fs of audio; sending to POC", len(pcm) / 2 / SAMPLE_RATE)
    try:
        data = do_turn(cfg, log, pcm, end_reason)
    except Exception as exc:  # noqa: BLE001 - POC failure must not kill the listener
        log.error("turn failed: %s (keeping listener alive)", exc)
        return
    log.info("POC transcript: %r stop=%s", data.get("transcript"), data.get("stop"))
    audio_b64 = data.get("audio_b64") or ""
    if data.get("stop") or not audio_b64:
        log.info("no audio reply (stop turn or silent turn)")
        return
    try:
        play_reply(base64.b64decode(audio_b64), log)
    except Exception as exc:  # noqa: BLE001 - playback failure must not kill the listener
        log.error("playback failed: %s", exc)


def selftest(cfg, log):
    ok = True
    try:
        model = load_wake_model()
        log.info("selftest: wake model loaded OK")
    except Exception as exc:  # noqa: BLE001
        log.error("selftest: wake model FAILED: %s", exc)
        return 1
    try:
        mic_index, mic_name = pick_mic(cfg["mic_substring"])
        log.info("selftest: mic [%d] %s", mic_index, mic_name)
    except SystemExit as exc:
        log.error("selftest: %s", exc)
        return 1
    try:
        with MicStream(mic_index) as mic:
            best = 0.0
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                frame = mic.read_frame(timeout=0.5)
                scores = model.predict(np.frombuffer(frame, dtype=np.int16))
                best = max(best, float(scores.get("hey_jarvis", 0.0)))
        log.info("selftest: mic read OK (3s, max hey_jarvis score %.3f)", best)
    except Exception as exc:  # noqa: BLE001
        log.error("selftest: mic read FAILED: %s", exc)
        ok = False
    try:
        status = get_json(cfg["poc_endpoint"] + "/api/status", timeout=10)
        log.info("selftest: POC /api/status OK: %s", status)
        if not status.get("ok"):
            ok = False
    except Exception as exc:  # noqa: BLE001
        log.error("selftest: POC unreachable: %s", exc)
        ok = False
    return 0 if ok else 1


def test_turn(cfg, log, wav_path):
    with open(wav_path, "rb") as f:
        wav_bytes = f.read()
    # The POC accepts any WAV; re-wrap raw PCM as WAV only if needed.
    if wav_bytes[:4] != b"RIFF":
        raise SystemExit("test-turn expects a WAV file")
    rate, channels, pcm = parse_wav(wav_bytes)
    log.info("test-turn: %s (%d Hz, %d ch, %.1fs)", wav_path, rate, channels,
             len(pcm) / 2 / rate / max(channels, 1))
    payload = {
        "session_id": str(uuid.uuid4()),
        "device": cfg["device"],
        "audio_b64": base64.b64encode(wav_bytes).decode("ascii"),
        "end_reason": "vad_end",
        "mode": "live",
    }
    status, data = post_json(cfg["poc_endpoint"] + "/api/turn", payload, timeout=70)
    log.info("test-turn: HTTP %s transcript=%r stop=%s",
             status, data.get("transcript"), data.get("stop"))
    audio_b64 = data.get("audio_b64") or ""
    if audio_b64:
        out = os.path.join(HERE, "reply.wav")
        with open(out, "wb") as f:
            f.write(base64.b64decode(audio_b64))
        log.info("test-turn: reply audio saved to %s (NOT played)", out)
    else:
        log.info("test-turn: no reply audio (stop turn or silent turn)")
    return 0 if status == 200 else 1


def main():
    ap = argparse.ArgumentParser(description="ROG wake-word listener for the Mercury POC")
    ap.add_argument("--list-mics", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--test-turn", metavar="WAV")
    args = ap.parse_args()

    cfg = load_config()
    log = setup_logging(cfg["log_file"])

    if args.list_mics:
        list_mics()
        return 0
    if args.selftest:
        return selftest(cfg, log)
    if args.test_turn:
        return test_turn(cfg, log, args.test_turn)
    try:
        listen_loop(cfg, log)
    except KeyboardInterrupt:
        log.info("stopped by user")
    return 0


if __name__ == "__main__":
    sys.exit(main())
