#!/usr/bin/env python3
"""Engine setup: download faster-whisper tiny model + Piper voice, verify
both without playing any audio. Writes marker files the server reads:
whisper_ready.txt, piper_ready.txt, voices/voice.txt (voice file name).

Voice order: en_US-joe-medium first, en_US-lessac-medium fallback.
No keys are needed or used here. Nothing is played aloud.
"""
import json
import subprocess
import sys
import urllib.request
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parent
VOICES = BASE / "voices"
VOICES.mkdir(exist_ok=True)
PIPER = BASE / ".venv" / "bin" / "piper"
HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "mercury-engine/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    dest.write_bytes(data)
    return len(data)


def main():
    # 1. Whisper tiny: load + transcribe 1s of silence (proves load).
    from faster_whisper import WhisperModel
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    silent = BASE / "silence.wav"
    with wave.open(str(silent), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    segments, _info = model.transcribe(str(silent))
    _text = "".join(s.text for s in segments)
    (BASE / "whisper_ready.txt").write_text("ok\n", encoding="utf-8")
    print("whisper tiny ready", flush=True)

    # 2. Piper voice: joe-medium, else lessac-medium.
    voice_name = None
    for speaker in ("joe", "lessac"):
        onnx = VOICES / f"en_US-{speaker}-medium.onnx"
        cfg = VOICES / f"en_US-{speaker}-medium.onnx.json"
        try:
            n1 = download(f"{HF}/en/en_US/{speaker}/medium/en_US-{speaker}-medium.onnx", onnx)
            n2 = download(f"{HF}/en/en_US/{speaker}/medium/en_US-{speaker}-medium.onnx.json", cfg)
            if n1 > 1000 and n2 > 100:
                voice_name = onnx.name
                break
        except Exception as exc:
            print(f"voice {speaker} download failed: {type(exc).__name__}", flush=True)
            continue
    if not voice_name:
        print("ERROR: no Piper voice downloaded", flush=True)
        sys.exit(1)
    (VOICES / "voice.txt").write_text(voice_name + "\n", encoding="utf-8")
    # 3. Synth to file only (no playback) to prove Piper works.
    out = BASE / "piper_test.wav"
    proc = subprocess.run(
        [str(PIPER), "--model", str(VOICES / voice_name),
         "--output_file", str(out)],
        input=b"Mercury Engine ready.", capture_output=True, timeout=120)
    if not out.exists() or out.stat().st_size == 0:
        print("ERROR: piper produced no audio", flush=True)
        sys.exit(1)
    (BASE / "piper_ready.txt").write_text("ok\n", encoding="utf-8")
    print(f"piper ready with voice {voice_name}", flush=True)


if __name__ == "__main__":
    main()
