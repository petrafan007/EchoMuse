# EchoMuse Mercury console (console)

The unified console at echo.petrafied.net: three tabs, one page.

- Prompts — Mercury prompt, function descriptions, spoken lines, memories,
  ask-Muse loop brief. Saving restarts Home Assistant.
- Commands — the matcher command list (39 commands at build time). Saving
  does not restart Home Assistant; the voice engine picks rules up itself.
- Settings — Mercury Engine status, keys and voice, browser voice test,
  browser Talk test. Proxies same-origin to the voice engine on port 8896:
  /api/engine-status, /api/engine-config, /api/engine-tts,
  /api/engine-transcribe, /api/engine-ask, /api/engine-stop.
  The old /api/poc-* paths are kept as aliases for cached pages.
  /api/turn is NOT proxied here; the Dot voice path is untouched.

Files: service.py (the console server, stdlib only),
prompt-editor.service (systemd unit template, LAN port 8898).

Access is gated by the Authentik embedded outpost in production; the
console itself has no login of its own. Keys typed here are sent only
when actually typed; a masked field keeps the stored key.
