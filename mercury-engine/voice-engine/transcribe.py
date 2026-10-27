#!/usr/bin/env python3
"""Transcribe one audio file with faster-whisper (distil-small.en, CPU int8).
Usage: python transcribe.py <audio-path>
Prints one JSON line: {"text": "..."} (last line of stdout is parsed).

Model picked Oct 25 2026 by benchmark (25 clips, in-process int8 CPU):
distil-small.en 25/25 adjusted vs small 22/25, at about 4.5s/clip vs 1.8s.
Same decode settings as the Engine server in-process path: language en pinned
(fix-forward Oct 24 2026: unpinned garbled audio hallucinated non-English),
vad_filter True with a loosened 0.3 threshold (default 0.5 dropped soft,
mumbled speech as non-speech), condition_on_previous_text False (turns stay
independent), hotwords for device words, temperature fallback for hard
segments. NOTE: initial_prompt was tried and REMOVED Oct 25 2026 after a
bisect proved it alone empties transcription on a clean clip (baseline ok,
prompt -> empty, hotwords ok, vad03 ok, temp-tuple ok).
"""
import json
import sys


def main():
    from faster_whisper import WhisperModel
    model = WhisperModel("distil-small.en", device="cpu", compute_type="int8")
    segments, _info = model.transcribe(
        sys.argv[1], language="en", vad_filter=True,
        vad_parameters={"threshold": 0.3},
        condition_on_previous_text=False,
        hotwords="lamp vacuum timer alarm Jarvis Muse",
        temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0))
    text = "".join(seg.text for seg in segments).strip()
    print(json.dumps({"text": text}))


if __name__ == "__main__":
    main()
