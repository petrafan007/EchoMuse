# Mercury Engine (voice-engine)

Formerly the Mercury POC. Renamed Oct 26, 2026, when the pipeline went
into full production cutover — it is proven, not a proof of concept.

## Run

Thinclient, systemd unit mercury-engine.service (template in this folder):
WorkingDirectory is the voice-engine directory on thinclient
(/home/user/mercury-engine), ExecStart runs server.py from its
venv. Port 8896 HTTP, 8444 TLS (same process, self-signed cert in tls/).
Port and TLS port can be overridden with MERCURY_ENGINE_PORT and
MERCURY_ENGINE_TLS_PORT.

## Endpoints (stable, unchanged by the rename)

- GET /api/status — booleans only, never key values.
- POST /api/turn — Dot voice turns, called by the fork controller.
- POST /api/ask — typed questions (iPhone Shortcut, console Talk test).
- POST /api/speak-clip — ask-Muse loop answers, played via Home Assistant.
- POST /api/stop, /api/session_start, /api/session_end.
- POST /api/matcher/test — dry run, no device action.
- GET /api/config is not public; the console proxies POST /api/config
  through its own Settings tab, sending only fields actually typed.

## Configuration

Copy config.example.json to engine_config.json next to server.py and
fill it in through the console UI. Keys are never printed or logged by
the server, and status endpoints report booleans only.

## Files

- server.py — the engine (stdlib only, plus uuid).
- transcribe.py — local faster-whisper decode settings.
- setup_models.py — downloads the local ASR model and Piper voice.
- matcher_rules.json — command matcher rules, reloaded on change.
- engine_ui.html — the engine's own browser UI (the console at
  echo.petrafied.net is the normal way to manage it).
- mercury-engine.service — systemd unit template.
