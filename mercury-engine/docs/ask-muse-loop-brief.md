# Ask-Muse voice loop — voice reply rules (standing)

These rules govern every Muse agent woken by the ask-Muse loop to answer a voice question. They are the voice contract the user specified Oct 24, 2026.

1. VOICE IS SUMMARY-ONLY. The Dot speaks only a short summary (a few sentences, under ~60 seconds), never the full long answer.
2. ATTRIBUTION FIRST. The spoken text MUST start with the exact words "Muse says:" followed by the summary.
3. VERBATIM, GOOGLE VOICE. The spoken text is exactly what the agent wrote. It goes to the POC's /api/speak-clip endpoint (Google Cloud TTS, WaveNet voice en-US-Wavenet-D) which returns a clip URL, and that URL is played on the Dot via HA media_player.play_media on media_player.office_voice_assistant — never Piper, and never a Piper fallback: if Google TTS fails, no answer is spoken and the failure is reported instead. No rephrasing by Mercury or anyone else. What the user hears is exactly what the agent wrote, in Google's voice.
4. FULL DETAIL GOES IN CHAT. Post the full detailed answer as the Q&A copy in the voice chat. The chat copy MUST contain the exact spoken summary (starting "Muse says:") plus the full answer, so the user can read exactly what was spoken.
5. LOOP BRIEF MIRROR. Before speaking, read the loop brief mirror on thinclient via the gateway (mcp_cli read /home/user/prompt-editor/loop-brief.md) and follow it if readable; if unreadable, follow this file. The mirror is what the user edits in the Prompts tab; edits there take effect on the next loop run.
6. NEVER SPEAK A DENIAL. Never say Muse cannot be reached, never say a tool is disabled, for a Muse-directed question. If the question is for Muse, the loop handles it; the agent's job is to answer, not to explain routing.
