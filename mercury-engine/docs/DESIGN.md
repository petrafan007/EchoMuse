# EchoMuse Controller Fork — Second Audio Sink Design

**Status: DESIGN + FEASIBILITY ONLY.** Written Oct 24, 2026 from read-only
investigation (upstream source/docs + thinclient read-only inspection). No live
controller, Dot, Home Assistant, or POC was modified, restarted, or played, and
no secret value was read, printed, or logged.

**Question answered:** the user's Echo Dot 2 cannot be *repointed* at the Mercury
POC with any stock setting (prior verdict, Oct 24 ~6:45 PM). This document designs
the alternative he approved exploring: **fork the EchoMuse controller and add a
second audio sink**, so a Dot turn can go to the POC instead of (or in addition
to) Home Assistant — with no change to the Dot's firmware.

**Caveats that bound this design:**
- The Dot ("Office") is **offline since 17:59 EDT Oct 24** (keepalive timeout,
  port 16001 down, ping 100% loss). That does not block this code-level design,
  but **live verification of any fork needs the Dot back** (cause: power, WiFi,
  or Dot crash — not established).
- The controller↔HA (ESPHome server) side is the least-documented part of the
  upstream docs. The device↔controller contract is documented exhaustively; the
  exact module that opens/drives the ESPHome voice session is **not named in the
  docs I read** and must be confirmed from source at build time. Everything in
  §2 that depends on it is marked accordingly.
- As-deployed facts (verified read-only Oct 24): controller runs the published
  image `ghcr.io/wilbowes/echomuse-controller:latest` (host networking,
  up 22h), thinclient dir holds only deploy files + `data/` (SQLite DB, TLS
  certs) — **there is no controller source on thinclient**; the fork is built
  from the upstream repo. Device firmware v2.18.1, emOS 0.1, bridge v2.26.2
  (per the earlier repoint check). Upstream repo: MIT, 1,107 commits, main at
  `f154031` at time of reading.

**Sources (upstream, public):**
- https://github.com/wilbowes/EchoMuse
- https://github.com/wilbowes/EchoMuse/blob/main/docs/voice-pipeline.md
- https://github.com/wilbowes/EchoMuse/blob/main/docs/device-controller-interface.md
- https://github.com/wilbowes/EchoMuse/blob/main/docs/listening.md

---

## 1. Code map: where a turn actually goes today

### 1.1 The three actors and what each owns

| Actor | Runs | Owns |
|---|---|---|
| Dot (Go firmware, `device/`) | emOS on the Dot 2 | Capture, wake word, AEC, beamforming, output chain, LED ring, buttons, ducking |
| Controller (Python, `controller/`) | thinclient Docker, host network | Conversation management, Silero speech gating, ESPHome server to HA, reply fetch + stream, dashboard |
| Home Assistant | thinclient Docker | STT → intent/conversation → TTS; media player, timers, announcements |

The key structural fact the whole design rests on: **the controller already
terminates both connections.** The Dot never talks to HA directly, and HA never
talks to the Dot directly. The controller is the only component that sees both
the device audio and the HA voice session. A fork therefore does *not* need to
impersonate HA, steal an ESPHome PSK, or touch the Dot — it only needs to add a
routing decision inside code it already owns. (The PSK problem from the earlier
"impersonate HA" idea applies only to the no-fork approach, and disappears
here.)

### 1.2 Device side (Go, `device/`)

- **Capture pipeline** (`device/internal/bindings/mic/pcm_microphone.go` and
  friends): 7 mics (6 ring + 1 centre), 16 kHz, 24-bit, +24 dB gain applied
  *before* conversion (this one change took STT from "fails 1 in 3" to
  reliable). Echo cancellation (SpeexDSP) subtracts the speaker's own audio
  from the mics. Beamforming: idle listening uses the centre mic; on wake it
  locks to the ring mic facing the speaker, chosen by looking *back* through a
  2-second directional-energy memory.
- **Wake word:** openwakeword, model `hey_jarvis`, threshold 0.5 (configurable).
  Default mode is **on-device** (`owwOnDevice=on`): nothing leaves the Dot
  until it fires. Controller-side wake mode exists per device (then the Dot
  streams mic audio continuously, every 32 ms). Capabilities that matter:
  `oww_shadow` (scores + reports, never acts) vs `oww_trigger` (acts on its own
  detection) — kept deliberately separate; `oww_local_only` (private listening);
  `wake_word_off` (HA "No wake word" picker); `aec_hw_ref`, `audio_mix`,
  `output_chain`, `led_anim`, `wake_cue`, `response_level`.
- **Output chain (device-side):** EQ → bass guard → limiter, applied at the
  ALSA write when both sides agree (`output_chain` in the controller's `ack`);
  never shaped twice. The Dot also ducks music itself the moment it hears its
  own wake word (music is already buffered inside the Dot, so only the device
  can duck in time).
- **Barge-in:** mics stay live during playback; AEC stops the Dot waking
  itself. On a barge-in wake the Dot **cuts its own reply at the crossing**
  (`listen.CutReplyOnBarge`) and opens a new session; up to ~210 ms of tail
  audio (ALSA ring + one period) still sounds and is sent.

### 1.3 Device ↔ controller wire protocol (the fork's playground)

Three WebSocket planes, all dialled by the device (discovered via mDNS
`_emcontroller._tcp.local`, or a static `controller.json`); plain `ws://` on
8767, TLS `wss://` on 8770 with a controller-generated CA; per-device token
in the `X-EM-Token` header on every dial (`controller/em_linkauth.py`:
`decide`, `follows_control`).

| Plane | Payload | Purpose |
|---|---|---|
| `/control` | JSON text, bidirectional | Registration, LEDs, mic start/stop, button/state events, config push, WiFi, shell control |
| `/data` | Binary, bidirectional | Mic PCM in; speaker + music PCM out |
| `/shell/{device_id}` | Raw binary, on demand | Root shell proxy (only after `shell_open`) |

**Control messages the fork cares about:**
- Device → controller: `register` (capabilities, board, base_os, boot_id,
  emmc health); `oww_wake` (score, threshold, ageMs, `capturedMono`, and
  under private listening `session`, `floor`, `barge`); `listen_state`
  (`local`/`stream`/`degraded`); `listen_end` (session, reason:
  `ack_timeout`, `max_open`, `muted`, `link`, `stopped`); `stats` (~30 s
  telemetry); `playback_stats` (delivery margin after a stream drains).
- Controller → device: `ack` (controller feature list: `ble_adverts_data`,
  `listen_session`, `output_chain`, `ble_connect`); `mic_start`
  (`lock_mic:false` = always-on wake stream, `true` = bounded VAD-gated turn);
  `mic_stop` (ends a bounded turn, **never** a session and never local
  listening — only `listen_close` ends a session); `listen_ack` (wake taken,
  stops the device's 3 s ack clock); `listen_close` (session, reason —
  ignored unless it is the open session); `beam_lock`/`beam_unlock`; `duck`
  (on/off); `speaker_flush` (**barge-in uses this**); `music_flush`;
  `leds`/`led_anim`; `volume_set`; `config` (partial-update push: mic gains,
  AEC, barge-in threshold, EQ/limiter/bass-guard keys, wake thresholds,
  `wakeArbitrationMs`, `duckDb`, `responseLevel`, `streamReply`,
  `saveUtterances` — several of these are controller-side keys, not device
  keys).

**Data frames (first byte = type; codes are namespaced by direction):**

| Direction | Code | Meaning |
|---|---|---|
| Controller → device | `0x02` | Speaker PCM chunk, played immediately |
| Controller → device | `0x03` | Speaker end-of-stream |
| Controller → device | `0x04` / `0x05` | Music PCM chunk / EOS (only if `audio_mix`) |
| Device → controller | `0x01` | Mic PCM chunk (mono `S16_LE`, 16 kHz) |
| Device → controller | `0x04` | VAD-end: bounded turn, speech detected then ended |
| Device → controller | `0x05` | No-speech timeout: bounded turn, no speech before timeout |
| Device → controller | `0x07` | Session audio: `[0x07][session u32 BE][seq u16 BE][PCM]` (private listening only) |
| Either | `0x08` | BLE GATT JSON (only if `ble_connect` negotiated) |

**Session lifecycle (private listening, the default path):**
1. Device scores wake locally → `oww_wake` with `session` id. Session audio
   starts **260 ms before** the crossing frame (2 s ring buffer on the device;
   the lead exists because scoring crosses a few frames into a command run on
   from the wake word).
2. Controller replies `listen_ack` (stops the device's 3 s `ackTimeout` clock).
3. `0x07` frames stream (session-tagged, sequenced; the controller holds
   frames for a not-yet-heard session up to `PENDING_MAX_S` and drops frames
   for closed sessions — this is what makes out-of-order control/data harmless).
4. Session ends by: controller `listen_close` at end-of-speech; controller
   declining the wake (arbitration loss, no HA connection, turn already
   running with barge-in off); device-side `ack_timeout` (3 s), `max_open`
   (30 s), mute, or link drop. Device-side limits are enforced on the Dot, so
   a crashed controller cannot leave it streaming.
5. Multi-device arbitration: **first to *hear* wins, not first to arrive**
   (capture-time dating via `capturedMono` + clock mapping, `em_listen.py`:
   `CaptureClock`, `DeviceClock`); claims within `wakeArbitrationMs`
   (default 700 ms) of the winner cede; a granted claim is never revoked.

### 1.4 Controller side (Python, `controller/`)

Modules cited by the upstream docs (all under `controller/`):
- `em_controller.py` — `Device` class; capability gates
  (`led_anim_capable`, `audio_mix_capable`, `button_hold_capable`,
  `oww_shadow_capable`, `oww_trigger_capable`, `aec_hw_ref_capable`).
- `em_listen.py` — listen-state resolution (`em_listen.resolve`), session
  handling, capture/clock mapping, `tail_discard`.
- `em_wakelevel.py` — wake loudness (dBFS, mic-gain removed) from the
  crossing frame; logged, not acted on.
- `em_linkauth.py` — link auth (`decide`, `follows_control`).
- Player module (logger `echomuse.player`) — reply fetch + stream to device.
- **ESPHome server module — filename NOT confirmed from docs; confirm from
  source at build time.** What is established: the controller exposes one
  ESPHome native-API listener per device (`EM_ESPHOME_PORT_BASE=16001`,
  Bluetooth proxies take base+1000; two controllers on one network need
  different bases), HA connects as an ESPHome client, and capability changes
  bounce the HA connection so the entity list refreshes
  (`controller/tests/test_capabilities.py`, `test_listen.py` pin this).

**Conversation management (the exact layer the fork hooks):**
- The controller runs a small Silero speech detector (ships inside the wake
  word package) over turn audio and **holds audio back until it hears speech**,
  then sends everything held in order — so HA gets exactly what it would have
  got, a fraction of a second later. A false wake / wake-then-silence sends
  nothing and ends quietly at the **5-second backstop** (judged against the
  room's measured noise floor).
- Endpointing is otherwise HA's VAD, with a controller backstop: if HA never
  engages (it needs ~1/3 s of confident speech before it can decide speech
  started, let alone stopped — the reason short commands like "stop" used to
  look slow), the controller ends the turn itself ~1 s after speech stops.
- **Reply path:** HA's spoken response is fetched by the controller from
  HA's `tts_proxy` URL and streamed to the Dot as `0x02` PCM chunks +
  `0x03` EOS. The satellite asks HA for the speaker's own format — **48 kHz
  mono WAV passthrough** (no decode); anything else is decoded with ffmpeg.
  The reply streams while HA is still generating it ("Speak while the reply
  is written" option, gated on HA's `tts_start_streaming` signal; off by
  default because a model/TTS slower than speech leaves gaps). The controller
  waits up to **30 s** for a reply to *begin* (`EM_TTS_WAIT_TIMEOUT`, max 120).
  While it plays, the ring throbs and clears when the Dot reports it actually
  finished (not on controller estimate). Music (media player, Music Assistant,
  radio) is decoded with ffmpeg and streamed a few seconds ahead; pause/stop
  are instant (buffer thrown away, not drained).
- Reference latency (upstream's own measurement, HA-Assist path, light
  command): wake → first frame 3 ms; speech 3.1 s; STT 1.8 s; intent 0.04 s;
  fetch 0.4 s; playback 2.2 s; **total 7.1 s**. STT is the stage that inflates
  on weak hardware (4.8 s median / up to 20.5 s on two shared cores vs 2.1 s
  median on four).

### 1.5 What stays on the device no matter what the fork does

Wake-word scoring, AEC, beamforming, mic gain, output chain (EQ/bass
guard/limiter), LED ring, ducking, button handling, barge-in cut, and the
session safety limits (3 s ack timeout, 30 s max open) are all device-side.
The fork changes **nothing** on the Dot: no firmware change, no re-pairing, no
config push beyond what the stock controller already sends. The Dot keeps
dialling the same three planes on the same ports; only the controller behind
them is forked.

---

## 2. The second sink: design

### 2.1 Insertion point (one paragraph, then the detail)

Hook the fork at the controller's conversation-management layer, **after**
wake + Silero gating and **before** the ESPHome voice session to HA is
opened. At that point the fork holds a complete, speech-gated turn (or a live
stream with a known session id) and has not yet committed it to any backend.
Routing is a decision about that turn object, not a rewiring of any
connection. The reply path is symmetric: instead of fetching a `tts_proxy`
URL, the fork streams POC reply audio as `0x02` chunks + `0x03` EOS on the
same `/data` plane, reusing the existing player code path.

```
Dot ──/control + /data──► FORKED CONTROLLER ──routing decision──┬──► HA (ESPHome voice session, stock path)
  wake (on-device)          wake + Silero gating                 │
  0x07 session audio        turn object + session id            ├──► POC (new sink, §3)
  0x02/0x03 reply audio     player: 0x02 chunks + 0x03 EOS       │
                                fallback on POC failure ─────────┘
```

Concretely, the fork adds (all controller-side, no device change):
1. **A sink interface** in the conversation manager: a backend is anything
   that accepts (session id, audio stream, end-of-turn signal) and returns
   (reply audio stream, stop handle). The HA backend is the existing
   implementation; the POC backend is the new one.
2. **A router** evaluated once per turn (before any backend is engaged),
   driven by the routing mode (§2.2) plus per-device config.
3. **A POC client** in the fork: sends the turn to the POC (§3) and streams
   the reply back through the existing player path, preserving the
   `0x02`/`0x03` framing, the output-chain negotiation, ducking
   (`duck` on/off around the turn), LED handling, and `speaker_flush` on
   stop/barge-in.
4. **A dashboard toggle** (per device, persisted in the fork's config):
   routing mode + POC endpoint + fallback behaviour. No new Dot firmware,
   no new pairing, no HA config change.

### 2.2 Routing options

| # | Mode | How it works | Cost / catch |
|---|---|---|---|
| A | **POC-first with HA fallback (RECOMMENDED)** | Every turn goes to the POC. If the POC is unreachable, errors, or times out (reply-wait budget, same 30 s shape as `EM_TTS_WAIT_TIMEOUT`), the fork opens the stock HA ESPHome voice session with the **buffered** turn audio (the fork already holds audio through Silero gating, so nothing is lost) and HA answers as today. | One extra failure path to test; fallback latency on POC outage is the POC timeout, so keep the POC timeout short (see §3.4). |
| B | **Manual toggle** | Per-device dashboard switch: POC / HA / POC-with-HA-fallback. the user flips it; no automatic behaviour. | Simplest to reason about; no automatic recovery if he forgets it on POC during an outage (mitigate: toggle defaults to A, not to POC-only). |
| C | **Per-turn phrase routing** | Fork transcribes first (via POC transcription), and a phrase set decides the backend: e.g. "ask Muse …" → POC, everything else → HA. | **Not recommended as the default.** It costs a transcription on every HA turn, and the HA path needs *audio*, not text — so the fork must buffer the whole turn and replay it into the HA session anyway. Workable (buffer-then-route makes it possible), but it is the most complex mode for the least benefit once mode A exists. Keep as a later option, not v1. |
| D | **Full cutover (POC-only)** | The fork never opens an HA voice session; every turn goes to the POC. | Cleanest end state *after* A has proven itself. Precondition to verify at build time: that the controller's ESPHome server can keep serving the media-player/timer/announcement entities while voice turns are diverted (see §5, risk 2). If voice and media are not separable in the ESPHome server, cutover also loses HA media player for the Dot — which may still be acceptable, since the POC can play audio itself, but it must be a conscious choice, not a surprise. |
| E | **Shadow mode (staging only, never a user-facing mode)** | Turns go to HA as today; the fork *also* sends the audio to the POC, which transcribes + runs Decide and **logs only** — never acts, never speaks. | Zero user impact; proves the audio path, transcription quality, and Decide routing on real turns before any POC turn is ever spoken. This is the recommended **first** staging step (§5.3). Sending to *both* as live backends is rejected: two answers, double actions, conflicting device commands. |

**Recommendation: ship B's toggle with A as the default** (POC-first with
HA fallback), prove it in E (shadow) first, and offer D (cutover) only after
A has run cleanly on real turns. Do not ship C in v1.

### 2.3 What happens to a turn, step by step (mode A)

1. Wake on-device → `oww_wake` (session id) → fork `listen_ack` → `0x07`
   session audio streams; Silero gating holds audio until speech; 5 s
   false-wake backstop unchanged.
2. Router decides POC (mode A default). No ESPHome voice session is opened
   to HA for this turn. (HA sees no voice turn; its media player, timers,
   and announcements are untouched.)
3. Fork sends the turn to the POC (§3). End-of-turn is signalled by the
   existing sentinels: `0x04` VAD-end, `0x05` no-speech timeout,
   `listen_close`, or the device-side 30 s `max_open` — the POC must honour
   all of them (§3.3), not invent its own endpointing.
4. POC replies (transcript + actions + audio). Fork streams audio as `0x02`
   chunks + `0x03` EOS through the existing player path: ducking on at turn
   start, ring throbs during playback, ring clears on device-reported finish.
5. Stop paths (all preserved, see §4.3): barge-in wake → Dot cuts its own
   reply + fork `speaker_flush` + drop remaining chunks; POC stop phrase →
   fork stop call (§3.4); turn timeout → fork closes the session and falls
   back or ends quietly per mode.
6. On POC failure (unreachable, error, timeout): fork opens the HA ESPHome
   voice session with the buffered turn audio and HA answers as today. The
   failure is logged with the turn id; the user hears an answer either way.

## 3. What the POC must implement (POC-side requirements)

The POC today (thinclient systemd service, port 8896) has: `GET /api/status`,
`POST /api/config`, `/api/transcribe` (audio → text, faster-whisper),
`/api/tts` (text → audio), `/api/ask` (text → Mercury 3 + HA REST actions +
reply), `/api/stop`, and a dormant `/api/voice-check`. The fork needs one new
contract; everything else is reuse.

### 3.1 New endpoint: turn-in, audio-out

Recommended shape (fork → POC, LAN HTTP; auth by LAN-only + a shared secret
header set in both configs — the POC UI today has no auth, LAN-only, so this
is consistent, not a downgrade):

- **Request:** `POST /api/turn` with the turn's audio and session metadata.
  Two acceptable variants, in order of preference:
  - (a) **Utterance-at-once (v1):** one POST carrying the complete turn
    audio (mono `S16_LE` 16 kHz WAV/PCM — exactly what arrives on `0x01` /
    `0x07`, no resampling) plus `{session_id, device, barge}`. Simplest,
    deterministic, matches the fork's existing buffering (Silero gating
    already holds audio; VAD-end gives a clean utterance boundary).
  - (b) **Streaming (v2):** chunked POST / WebSocket carrying live PCM with
    an end-of-turn message, and audio chunks streamed back as TTS generates
    (the POC-side analogue of "speak while written"). Faster first-audio,
    but more moving parts; do it after (a) proves the loop.
- **Response (v1):** reply audio bytes + a small JSON trailer (or multipart):
  transcript, Decide verdict, actions taken, engine used (Piper / WaveNet),
  and whether the turn was a stop. The fork needs the transcript for its
  turn history and the engine label for the dashboard, not just the audio.
- **Stop:** `POST /api/stop` already exists — the fork calls it when a
  barge-in wake arrives or a POC stop phrase is detected, and the fork
  itself drops remaining `0x02` chunks + sends `speaker_flush`. Both halves
  are required: POC-side stop alone cannot silence audio already buffered
  on the Dot, and fork-side flush alone cannot stop POC-side generation.

### 3.2 Audio formats (the one hard constraint)

- **In (Dot → fork → POC):** mono `S16_LE` 16 kHz. No conversion needed:
  this is already the POC's faster-whisper input shape.
- **Out (POC → fork → Dot):** the speaker path today is **48 kHz mono WAV
  passthrough** (no decode). POC TTS today emits Piper WAV at 22050 Hz and
  Google WaveNet MP3. The fork must therefore transcode POC audio to
  48 kHz mono WAV (ffmpeg — the controller already shells to ffmpeg for
  music) **or** the POC must add a 48 kHz WAV output option. Either is
  fine; what is *not* fine is sending 22050 Hz WAV or MP3 down the `0x02`
  path and hoping the Dot sorts it out — the passthrough path assumes the
  speaker's own format, and the ffmpeg-decode fallback path is what used to
  hold up to 1.7 s of speech inside its decoder (the exact bug that forced
  the WAV-passthrough change). Format negotiation must be explicit in the
  POC contract, and the output-chain (EQ/bass guard/limiter) negotiation
  must stay exactly as today (device-side when both sides agree, never
  shaped twice).

### 3.3 Turn-end semantics the POC must honour (not invent)

The POC's current input is browser push-to-talk / typed text — it has no
endpointing of its own. On the Dot it must accept the fork's end-of-turn
signals as authoritative: `0x04` VAD-end (speech then ended — normal case),
`0x05` no-speech timeout (wake then silence — end quietly, no reply),
`listen_close` (controller closed the session), device `max_open` 30 s
(hard cap — a turn longer than this is cut regardless), and the fork's
~1 s post-speech endpoint backstop. The POC must also accept a
barge-in session (new session id while a reply plays) as a *replacement*
turn, not an error. Getting this wrong is how turns hang, cut early, or
double-answer — it is the second-biggest technical risk (§5.1).

### 3.4 Timeouts, failure, and logging

- POC reply-wait budget: mirror the HA shape — a short first-audio
  timeout (recommend starting at the same 30 s as `EM_TTS_WAIT_TIMEOUT`,
  tunable down once real latencies are measured) after which mode A falls
  back to HA. Never let a POC outage silence a turn.
- Per-turn log (fork side, same shape as today's turn history): session id,
  routing decision, transcript, Decide verdict + confidence, backend used,
  fallback (if any), latencies (wake → first audio frame, STT, LLM, TTS,
  first speaker chunk), and stop/barge events. This log is what Phase 2
  tuning (§4.4) runs on.
- No secrets in either log: transcripts are fine; keys, tokens, and PSKs
  never are (the fork already follows this rule for Sendspin tokens).

## 4. Ceiling: how far the fork can take it

### 4.1 The full POC on the Dot — yes, end to end

With the fork, the complete POC loop runs on the Dot with **no HA voice
pipeline in the turn at all**:

```
wake (on-device, unchanged) → fork routes → POC faster-whisper (16 kHz mono,
no resample) → POC Decide router → Mercury 3 → POC HA REST actions →
POC TTS (Piper / WaveNet) → fork transcodes to 48 kHz mono WAV →
0x02 chunks + 0x03 EOS → Dot speaker (output chain, ring, ducking intact)
```

Every POC behaviour carries over: the deterministic keyword stop check (now
*stronger* than the browser POC, because the fork owns playback and can
flush it instantly); Decide routing with HA fallback; Mercury 3 function
calls executed via the POC's HA REST API; ask-Muse relay (the POC already
writes the `ASK_CLAUDE` marker via HA's API — unchanged).

### 4.2 What still needs HA in the loop

HA remains, but only as **device layer and optional media layer**, never as
brain:
- Device actions (lights, vacuum, Kia, cameras, routines): via the POC's
  existing HA REST API calls — same as the browser POC today.
- Media player / timers / announcements for the Dot: unchanged *if* the
  fork keeps the ESPHome server running and only diverts voice turns
  (to verify at build time, §5.1 risk 2). If they prove inseparable, the
  choice is conscious: keep HA voice for media-adjacent turns, or accept
  the POC owning playback and lose HA timers/announcements on the Dot.
- The ask-Muse voice loop's HA automation + hook: untouched (the POC
  writes the same marker; the hook still wakes a Muse agent the same way).
- Nothing in a POC turn needs HA's Assist pipeline, Wyoming containers,
  Extended OpenAI Conversation, or Mercury-via-HA prompt rules. The Wyoming
  TTS failure mode seen Oct 24 (17:30:37 `# channels not specified`) is
  bypassed entirely, because POC TTS is local Piper / Google, not Wyoming.

### 4.3 What stays working untouched

Wake word (on-device, same model + threshold), AEC, beamforming, mic gain,
output chain, LED ring + animations, ducking, button events, mute (device-
sovereign, software), multi-Dot arbitration (first-to-hear wins — the fork
keeps this logic; with one Dot it is a no-op), and barge-in (device cuts
its own reply; fork flushes). **Deterministic stop gets better, not
worse:** today's HA-path failure was Mercury never issuing `media_stop`
(LLM non-determinism). In the fork world the stop is code, not a model
decision: POC stop phrase → fork stop call → `speaker_flush` + drop
remaining chunks, and barge-in wake → device-side cut. The POC's keyword
pre-check (exact stop phrases never touch the internet) survives intact —
the fork is on the LAN, so a local stop works even if Inception is down.

### 4.4 What this is not

- Not a Dot firmware change: no emOS rebuild, no re-pairing, no new wake
  model. (Custom wake words via `oww_forge` remain available exactly as
  today, through the fork's dashboard.)
- Not a second HA: the fork does not reimplement Assist, intents, or
  Wyoming. It routes audio and plays audio; brains live in the POC.
- Not Phase 2 by itself: Phase 2 (tuning from real logs) still follows —
  the fork's per-turn log (§3.4) is what makes Phase 2 possible on the Dot.

## 5. Costs and risks, honestly

### 5.1 Top 3 risks

1. **The ESPHome-server internals are the least-documented seam.**
   The device↔controller contract is documented to the frame byte, but the
   controller↔HA voice-session code (session open/drive, reply-audio
   sequencing, `tts_start_streaming` handling, the 30 s reply-wait) is only
   sketched in docs. The fork's diff is small *if* the voice path is cleanly
   separable from the ESPHome server; if it is tangled, the diff grows and
   so does merge cost. Mitigation: read that module first at build time,
   before committing to a design detail; the router must be a thin layer,
   not a rewrite of the session code.
2. **Voice/media separability in the ESPHome server is unverified.**
   If diverting voice turns also breaks (or confuses) the media-player,
   timer, and announcement entities HA exposes for the Dot, full cutover
   (mode D) has a real cost. Mitigation: mode A keeps HA sessions working
   (fallback exercises the path on every POC failure), and cutover is
   gated on a live test proving media still works with voice diverted.
3. **Turn-end semantics are easy to get subtly wrong.**
   VAD-end vs no-speech-timeout vs listen_close vs 30 s max_open vs the
   ~1 s endpoint backstop: mishandling any one produces hangs, cut-off
   commands ("stop" heard as "…op"), or double answers. Mitigation: the
   fork must treat the existing sentinels as authoritative (§3.3) and the
   first live tests must cover each sentinel explicitly (wake-then-silence,
   short "stop", long command hitting max_open, barge-in mid-reply).

### 5.2 Fork maintenance vs upstream

- **Diff discipline is the whole game.** Keep the fork's change surface
  minimal and isolated: one routing module + one dashboard toggle + the
  POC client. Everything else stays stock. A small diff rebases cleanly;
  a rewrite does not.
- **Merge cadence:** track upstream `main` (it moves fast: 1,107 commits,
  74 releases, active daily use by the maintainer). Recommended: rebase or
  merge on each upstream minor release, run the controller test suite
  (`test_capabilities.py`, `test_listen.py` — the contract guard rails)
  plus a live-Dot smoke test before deploying a merged build.
- **OTA / firmware path:** the fork must decide what serves device
  firmware updates. Recommended: **keep firmware updates on upstream
  releases** (the controller fetches them from GitHub; capability
  negotiation means upstream firmware interoperates with a forked
  controller, and vice versa — that negotiation exists precisely so mixed
  versions work). The fork publishes controller-only releases. Verify at
  build time how the update check is configured; if it is hard-wired to
  the fork's repo, point it at upstream explicitly. Do *not* fork the
  firmware unless a device-side change is ever actually needed — none is
  needed for the second sink.
- **Rollback (fast, no Dot touch):** back up the stock compose file +
  `data/` dir before first fork deploy. Rollback = stop the fork container,
  restart the stock image with the same data dir and compose. The Dot
  re-registers on every redial, so it reconnects to the stock controller
  with no re-pairing — *provided the fork never migrates the SQLite schema
  incompatibly*. Rule: the fork makes no schema changes, or the backup is
  taken after every schema change it does make. Test rollback once, on
  purpose, before relying on it.

### 5.3 Staging without touching the working HA path

1. **Shadow mode (E) first, on the live Dot:** fork deployed with routing
   = shadow. HA answers every turn exactly as today; the POC receives
   turn audio, transcribes, runs Decide, and logs — never acts, never
   speaks. This proves the audio path, transcription quality, and Decide
   routing on real turns with zero user impact. Exit criterion: POC
   transcripts match HA's transcripts on real turns, and Decide routing
   matches expectations.
2. **POC-first with HA fallback (A), toggleable:** flip one device to A.
   Every POC failure still ends in an HA answer (buffered audio), so the
   worst case of a POC outage is a slower answer, not silence. Exit
   criterion: POC turns complete end-to-end (including stop + barge-in)
   and fallback fires correctly on a forced POC outage.
3. **Cutover (D) only after A is clean:** POC-only for the Dot, with media
   behaviour re-verified (§5.1 risk 2). Keep the toggle: cutover is a
   config value, not a rebuild.
4. **What does *not* work as staging:** running a second forked controller
   alongside the live one and expecting the Dot to pick it. The Dot dials
   one controller (mDNS `_emcontroller._tcp.local` or static
   `controller.json`); two controllers advertising on one network is
   undefined behaviour for device selection, and two controllers need
   different ESPHome bases anyway. A second *Dot* (the user has considered
   buying more) is the clean multi-controller test: point the spare Dot
   at the fork, keep Office on stock. With one Dot, the toggle + shadow
   sequence above is the safe staging path.

### 5.4 Build order (when the user says go)

1. Read the ESPHome-server module + player module from source; confirm
   the insertion point and the voice/media separability question.
2. Build the sink interface + router + dashboard toggle (no POC changes
   yet); deploy in shadow mode; verify transcripts + Decide logs.
3. Build the POC `/api/turn` endpoint (utterance-at-once v1) + 48 kHz WAV
   output (or fork-side ffmpeg transcode — decide at build time by
   measuring which is lower-latency); wire the POC backend into the fork.
4. Flip to POC-first with HA fallback; test every sentinel + stop +
   barge-in + forced-outage fallback explicitly.
5. Measure: wake → first audio frame, STT, Decide, LLM, TTS, first speaker
   chunk, stop latency. Those numbers are the Phase 2 tuning baseline.
6. Only then: cutover decision (mode D), with media re-verified.

### 5.5 Open questions for build time (not blockers for this design)

- ESPHome-server module filename + exact voice-session open/drive calls.
- Whether the controller's firmware-update check can stay pointed at
  upstream releases in a forked build.
- Whether voice diversion affects media-player/timer/announcement entities
  (test, §5.1 risk 2).
- 48 kHz WAV: POC-side output option vs fork-side ffmpeg transcode —
  measure both, pick lower latency.
- His firmware (v2.18.1) vs the session protocol: the live 17:38 EDT turn
  proves sessions work today, and the fork changes nothing device-side,
  so no firmware risk is expected — but confirm `listen_session` /
  `0x07` handling on his exact firmware during shadow testing.
- The Dot must be back online before any live step (offline since
  17:59 EDT Oct 24; cause unknown).

---

*End of design. Next step is the user's: build (in the order of §5.4), or
revise the routing recommendation (§2.2).*
