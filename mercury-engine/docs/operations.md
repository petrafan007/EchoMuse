# Mercury Engine operations (runbook)

## Services (thinclient)

- mercury-engine — voice engine, ports 8896 (HTTP) and 8444 (TLS).
  Restart: sudo -n systemctl restart mercury-engine
  Status: /api/status (booleans only)
- prompt-editor — console, port 8898 (LAN). Restart:
  sudo -n systemctl restart prompt-editor
- Fork controller (live, untouched by the Oct 26 rename):
  container echomuse-controller-fork-live, route poc_only,
  endpoint http://127.0.0.1:8896. Its route name and EM_POC_ENDPOINT
  are legacy names kept on purpose; renaming them is a separate
  full-rename job with an image rebuild and Dot re-register.

## Verify after a restart (no Dot live tests without the user's go)

- Engine: GET /api/status, all keys true, voice google/en-US-Wavenet-D
  (or whatever the console shows), whisper_ready and piper_ready true.
- Console: GET /health, /api/prompts, /api/commands, /api/engine-status;
  /api/poc-status must still answer as a legacy alias.
- Matcher dry run only: POST /api/matcher/test with a phrase
  (for example, turn the lamp on) — it reports what it would do and
  never touches a device. Never verify with a live Dot turn that
  changes a device unless the user asked for it first.
- Engine config no-op: POST an empty object through the console's
  /api/engine-config and confirm keys and voice are unchanged.

## Secrets

Never commit engine_config.json, TLS keys, Home Assistant tokens,
webhook IDs, or API keys. Status endpoints report booleans only.
If a key ever lands in a public commit, rotate it — deleting the
commit does not un-leak it.

## Upstream

Fork base: upstream f154031, fork head 419626a at the time of the
Oct 26 move. New folders under mercury-engine/ never conflict with
upstream merges; the controller files the user modified are where a
future upstream merge needs attention. Merge upstream selectively
(security fixes, a board you own, Home Assistant compatibility),
not blindly on every upstream commit, and re-run the fork test suite
and a live Dot check after any merge.
