# Home Assistant fragments (home-assistant)

These are fragments and templates for the user's Home Assistant, not
drop-in whole configuration files. Home Assistant's .storage files are
never committed, and no tokens or webhook IDs are included — replace
placeholders with your own values.

- lovelace-echomuse-mercury-view.json — the Overview second view
  ("EchoMuse Mercury", panel view, single iframe card pointing at the
  console). Merge this view into your lovelace configuration; do not
  replace your whole dashboard with it.
- automations-ask-muse.yaml — the ask-Muse voice-loop automations
  (voice trigger and webhook relay), which write ASK_CLAUDE|<question>
  to the Home Assistant log for the loop to pick up. Create a webhook
  in Home Assistant and put its ID in place of the placeholder.
- scripts-voice.yaml — voice-related scripts (sleep sounds, reminders,
  camera snapshots). Merge the scripts you need into scripts.yaml.

Voice devices and entities referenced here (Office Dot as
media_player.office_voice_assistant, switch.lamp, vacuum.s8, timers,
alarm helpers) are the user's own Home Assistant setup; adapt entity IDs
to yours.
