# ROG wake-word listener (rog-wake)

Live file: D:\Muse\rog-wake\rog_wake.py on the ROG (Windows), autostarted
via a Startup VBS. The live script is included here as rog_wake.py,
fetched from the ROG Oct 26, 2026 and SHA256-verified; this README
records the contract.

Contract: openWakeWord hey_jarvis (onnx, threshold 0.5), microphone
pinned to the desk mic, capture on wake, POST the audio to the voice
engine's /api/turn on thinclient port 8896, play the 48 kHz reply.
Stop turns stay silent. Deliberate v1 limits: the ROG must be awake,
desk range only (near-field mic), and it makes no session_start/end
calls, because the engine's media-pause flag is single-global and a
ROG session must not resume Dot media mid-Dot-turn.

To add the live script here: copy rog_wake.py from the ROG, replace
any LAN/Tailscale address with a placeholder in this copy, and keep
the ROG original as the running version.
