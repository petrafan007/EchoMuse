# EchoMuse Mercury — Mercury Engine project

This folder is the user's side of the EchoMuse Mercury voice system, added to the
true fork of wilbowes/EchoMuse (petrafan007/EchoMuse) so the whole project
lives in one GitHub project. The fork's controller code (controller/,
device/, docs/, etc.) is upstream's work plus the user's voice-routing diff;
everything under mercury-engine/ is the user's own code and configuration.

## What lives here

- voice-engine/ — Mercury Engine, the voice pipeline that replaced the
  proof-of-concept name on Oct 26, 2026. Audio in, transcription (Groq-first,
  local distil-small fallback), matcher, Decide routing, Mercury 3 brain,
  Home Assistant REST actions, ding/TTS out. Runs on thinclient, port 8896
  (HTTP) and 8444 (TLS, same service). The Dot voice path is the fork
  controller calling this service's /api/turn.
- console/ — EchoMuse Mercury unified console (Prompts / Commands / Settings),
  served at echo.petrafied.net through the Authentik embedded outpost, and on
  the LAN at port 8898. Prompts saves restart Home Assistant; Commands saves
  do not; Settings proxies to the voice engine and never touches /api/turn.
- home-assistant/ — Fragments and templates for the Home Assistant side:
  the EchoMuse Mercury dashboard view, the ask-Muse voice-loop automations,
  and the voice-related scripts. These are fragments, not drop-in whole
  files — see home-assistant/README.md. Home Assistant's .storage files are
  never committed.
- rog-wake/ — The ROG wake-word listener (openWakeWord hey_jarvis) that
  posts captured audio to the voice engine's /api/turn. See rog-wake/README.md.
- docs/ — DESIGN.md (the original fork design) and the ask-Muse loop brief,
  plus operations.md (runbook).

## What is deliberately NOT here

- engine_config.json (API keys and the Home Assistant long-lived token).
  Copy voice-engine/config.example.json to engine_config.json on thinclient
  and fill it in through the console UI. Never commit the real file.
- TLS private keys, model weights, Piper voice files, audio clips, logs,
  .bak files, __pycache__, and any .storage directory. See .gitignore.
- The live thinclient directories themselves. This repo is the project
  home and backup; thinclient remains the running system. Do not delete
  thinclient files after pushing here.

## Legacy names kept on purpose

- The fork controller still uses the route name poc_only and the env var
  EM_POC_ENDPOINT. Renaming those is a separate full-rename job (fork code,
  image rebuild, Dot re-register) and was explicitly left out of the
  Oct 26 Mercury Engine rename, which covered the thinclient service only.
- The console still accepts the old /api/poc-* paths as aliases for
  /api/engine-*, so cached pages keep working.
- Log filenames poc.log and poc-turns.jsonl are kept, because the voice-log
  consolidator reads poc-turns.jsonl by name.

## Upstream note

Upstream base at the time of this move: f154031, with fork commits through
419626a (follow-up flag). Merging upstream later touches the controller
files the user modified (em_controller.py, config files, dashboard), not the
mercury-engine/ folders, which upstream never touches. See docs/operations.md.
