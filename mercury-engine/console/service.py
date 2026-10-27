#!/usr/bin/env python3
"""Prompt editor service for the Home Assistant Prompts tab.

Serves the editor page the user uses in the Prompts dashboard view and applies
saves to the real Home Assistant configuration. Trust model: no auth; reachable on the LAN and via the
echo.petrafied.net tunnel (the same exposure as the prompt editor
itself). Command saves do not restart Home Assistant.

Save behavior (the user's explicit words): every Save applies (backup +
validate) and then restarts Home Assistant, then verifies. If verification
fails, backups are restored and Home Assistant is restarted again.

This service never prints or serves secret values. The functions editor
exposes only per-function descriptions, never headers, payloads, or keys.
"""

import json
import os
import re
import subprocess
import threading
import urllib.request
import urllib.error
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HA_DIR = "/var/lib/docker/volumes/homeassistant_config/_data"
CONFIG_ENTRIES = HA_DIR + "/.storage/core.config_entries"
AUTOMATIONS = HA_DIR + "/automations.yaml"
SCRIPTS = HA_DIR + "/scripts.yaml"
MEMORIES = "/home/user/mercury-memory/memories.json"
LOOP_BRIEF = os.path.join(BASE_DIR, "loop-brief.md")
PORT = 8898
LOCK = threading.Lock()


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M")


def run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def sudo_cp(src, dst):
    r = run(["sudo", "cp", src, dst])
    if r.returncode != 0:
        raise RuntimeError("sudo cp failed: " + r.stderr[:300])
    return True


def backup_ha_file(path):
    bak = path + ".bak-" + stamp() + "-prompts"
    r = run(["sudo", "cp", path, bak])
    if r.returncode != 0:
        raise RuntimeError("backup failed for " + path + ": " + r.stderr[:300])
    return bak


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def find_chatgpt(data):
    for entry in data.get("data", {}).get("entries", []):
        if entry.get("title") == "ChatGPT":
            subs = entry.get("subentries", [])
            if subs:
                return subs[0].get("data", {})
    raise RuntimeError("ChatGPT entry not found")


def find_ollama_prompt(data):
    for entry in data.get("data", {}).get("entries", []):
        if entry.get("domain") == "ollama":
            subs = entry.get("subentries", [])
            if subs:
                return subs[0].get("data", {}).get("prompt", "")
    return ""


def split_function_blocks(funcs):
    parts = funcs.split("- spec:")
    blocks = []
    for part in parts[1:]:
        m = re.search(r"name:\s*(\S+)", part[:500])
        name = m.group(1) if m else "UNKNOWN"
        blocks.append((name, part))
    return blocks


def spec_description(block_text):
    for line in block_text.split("\n"):
        if line.startswith("    description:"):
            val = line.split(":", 1)[1].strip()
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                try:
                    return json.loads(val)
                except Exception:
                    return val.strip('"')
            if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
                return val[1:-1]
            return val
    return ""

ANCHORS = {
    "frigate": "trigger.json.message",
    "reminder": "input_text.reminder_message",
    "goodnight": "Good night. The lamp is off",
    "movie": "Movie night. Lights off",
}


def unquote(val):
    val = val.strip()
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        try:
            return json.loads(val)
        except Exception:
            return val.strip('"')
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1]
    return val


def extract_message(text, anchor):
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("message:") and anchor in line:
            return unquote(s.split(":", 1)[1])
    raise RuntimeError("message anchor not found: " + anchor)


def replace_message(text, anchor, new_value):
    clean = " ".join(str(new_value).split())
    lines = text.split("\n")
    idx = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("message:") and anchor in line:
            idx = i
            break
    if idx is None:
        raise RuntimeError("message anchor not found: " + anchor)
    indent = lines[idx][:len(lines[idx]) - len(lines[idx].lstrip())]
    lines[idx] = indent + "message: " + json.dumps(clean)
    out = "\n".join(lines)
    old_lines = text.split("\n")
    diffs = [i for i, (a, b) in enumerate(zip(old_lines, out.split("\n"))) if a != b]
    if diffs not in ([idx], []):
        raise RuntimeError("unexpected diff in message replace")
    return out


def extract_morning(text):
    lines = text.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line == "morning_briefing:":
            start = i
            break
    if start is None:
        raise RuntimeError("morning_briefing section not found")
    msg_idx = None
    for i in range(start + 1, len(lines)):
        if lines[i] and not lines[i][0].isspace():
            break
        if lines[i].strip().startswith("message:"):
            msg_idx = i
            break
    if msg_idx is None:
        raise RuntimeError("morning message line not found")
    body = []
    for line in lines[msg_idx + 1:]:
        if line.strip() == "":
            body.append("")
            continue
        if len(line) - len(line.lstrip()) >= 10:
            body.append(line[10:] if line.startswith(" " * 10) else line.lstrip())
        else:
            break
    while body and body[-1] == "":
        body.pop()
    return "\n".join(body)


def replace_morning(text, new_value):
    lines = text.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line == "morning_briefing:":
            start = i
            break
    if start is None:
        raise RuntimeError("morning_briefing section not found")
    msg_idx = None
    for i in range(start + 1, len(lines)):
        if lines[i] and not lines[i][0].isspace():
            break
        if lines[i].strip().startswith("message:"):
            msg_idx = i
            break
    if msg_idx is None:
        raise RuntimeError("morning message line not found")
    indent = lines[msg_idx][:len(lines[msg_idx]) - len(lines[msg_idx].lstrip())]
    lines[msg_idx] = indent + "message: >"
    end = msg_idx + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() == "":
            end += 1
            continue
        if len(line) - len(line.lstrip()) >= 10:
            end += 1
        else:
            break
    new_body = []
    for raw in str(new_value).split("\n"):
        if raw.strip() == "":
            new_body.append("")
        else:
            new_body.append(" " * 10 + raw)
    while new_body and new_body[-1] == "":
        new_body.pop()
    out_lines = lines[:msg_idx + 1] + new_body + lines[end:]
    res = "\n".join(out_lines)
    res_lines = res.split("\n")
    assert res_lines[:msg_idx] == lines[:msg_idx], "morning prefix changed"
    assert res_lines[msg_idx + 1 + len(new_body):] == lines[end:], "morning suffix changed"
    return res


def load_memories():
    if not os.path.exists(MEMORIES):
        return []
    data = load_json(MEMORIES)
    return data if isinstance(data, list) else []


def get_prompts():
    data = load_json(CONFIG_ENTRIES)
    sub = find_chatgpt(data)
    funcs = sub.get("functions", "")
    functions = []
    for name, block in split_function_blocks(funcs):
        functions.append({
            "name": name,
            "description": spec_description(block),
            "has_value_template": "value_template" in block,
            "has_payload_template": "payload_template" in block,
        })
    auto_text = load_text(AUTOMATIONS)
    script_text = load_text(SCRIPTS)
    lines = {}
    lines["alarm"] = "Sound: /local/sounds/timer-ding-60s.mp3 (60s ding)"
    lines["timer"] = "Sound: /local/sounds/timer-ding-60s.mp3 (60s ding)"
    lines["frigate"] = extract_message(auto_text, ANCHORS["frigate"])
    lines["reminder"] = extract_message(auto_text, ANCHORS["reminder"])
    lines["goodnight"] = extract_message(script_text, ANCHORS["goodnight"])
    lines["movie"] = extract_message(script_text, ANCHORS["movie"])
    lines["morning"] = extract_morning(script_text)
    try:
        loop_brief = load_text(LOOP_BRIEF)
    except Exception:
        loop_brief = ""
    return {
        "prompt": sub.get("prompt", ""),
        "prompt_len": len(sub.get("prompt", "")),
        "functions": functions,
        "ollama_prompt": find_ollama_prompt(data),
        "lines": lines,
        "memories": load_memories(),
        "loop_brief": loop_brief,
    }


def replace_descriptions(funcs, desc_map):
    parts = funcs.split("- spec:")
    out = [parts[0]]
    for part in parts[1:]:
        m = re.search(r"name:\s*(\S+)", part[:500])
        name = m.group(1) if m else "UNKNOWN"
        if name in desc_map:
            lines = part.split("\n")
            idx = None
            for i, line in enumerate(lines):
                if line.startswith("    description:"):
                    idx = i
                    break
            if idx is None:
                raise RuntimeError("description line not found for " + name)
            lines[idx] = "    description: " + json.dumps(str(desc_map[name]))
            new_part = "\n".join(lines)
            old_lines = part.split("\n")
            new_lines = new_part.split("\n")
            diffs = [i for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
            if diffs not in ([idx], []):
                raise RuntimeError("unexpected diff in description replace for " + name)
            part = new_part
        out.append(part)
    return "- spec:".join(out)


def find_ollama_subdata(data):
    for entry in data.get("data", {}).get("entries", []):
        if entry.get("domain") == "ollama":
            subs = entry.get("subentries", [])
            if subs:
                return subs[0].get("data", {})
    return None


def ha_running():
    r = run(["docker", "ps", "--format", "{{.Names}}"], timeout=30)
    return "homeassistant" in r.stdout.split()


def restart_ha():
    r = run(["docker", "stop", "homeassistant"], timeout=120)
    if r.returncode != 0:
        raise RuntimeError("docker stop failed: " + r.stderr[:300])
    for _ in range(30):
        if not ha_running():
            break
        time.sleep(2)
    if ha_running():
        raise RuntimeError("homeassistant did not stop")
    r = run(["docker", "start", "homeassistant"], timeout=120)
    if r.returncode != 0:
        raise RuntimeError("docker start failed: " + r.stderr[:300])
    time.sleep(35)
    if not ha_running():
        raise RuntimeError("homeassistant did not start")


def verify_window(minutes):
    cutoff = time.localtime(time.time() - minutes * 60)
    cutoff_s = time.strftime("%Y-%m-%d %H:%M", cutoff)
    try:
        text = load_text(HA_DIR + "/home-assistant.log")
    except Exception:
        return False
    recent = [l for l in text.split("\n") if len(l) >= 16 and l[:16] >= cutoff_s]
    bad = [l for l in recent if "FunctionLoadFailed" in l]
    return len(bad) == 0


def apply_save(payload):
    with LOCK:
        data = load_json(CONFIG_ENTRIES)
        sub = find_chatgpt(data)
        ollama_sub = find_ollama_subdata(data)
        auto_text = load_text(AUTOMATIONS)
        script_text = load_text(SCRIPTS)
        new_auto = auto_text
        new_script = script_text
        line_items = payload.get("lines", {}) or {}
        for key in ("frigate", "reminder"):
            if key in line_items:
                new_auto = replace_message(new_auto, ANCHORS[key], line_items[key])
        for key in ("goodnight", "movie"):
            if key in line_items:
                new_script = replace_message(new_script, ANCHORS[key], line_items[key])
        if "morning" in line_items:
            new_script = replace_morning(new_script, line_items["morning"])
        new_prompt = payload.get("prompt", None)
        new_funcs = None
        if payload.get("functions") is not None:
            new_funcs = replace_descriptions(sub.get("functions", ""), payload["functions"])
        new_ollama = payload.get("ollama_prompt", None)
        new_memories = payload.get("memories", None)
        new_loop = payload.get("loop_brief", None)
        config_changed = False
        if new_prompt is not None and new_prompt != sub.get("prompt", ""):
            sub["prompt"] = new_prompt
            config_changed = True
        if new_funcs is not None and new_funcs != sub.get("functions", ""):
            sub["functions"] = new_funcs
            config_changed = True
        if new_ollama is not None and ollama_sub is not None and new_ollama != ollama_sub.get("prompt", ""):
            ollama_sub["prompt"] = new_ollama
            config_changed = True
        auto_changed = new_auto != auto_text
        script_changed = new_script != script_text
        mem_changed = False
        if new_memories is not None:
            if new_memories != load_memories():
                mem_changed = True
        loop_changed = False
        if new_loop is not None:
            try:
                old_loop = load_text(LOOP_BRIEF)
            except Exception:
                old_loop = ""
            if new_loop != old_loop:
                loop_changed = True
        backups = []
        if config_changed:
            backups.append((CONFIG_ENTRIES, backup_ha_file(CONFIG_ENTRIES)))
        if auto_changed:
            backups.append((AUTOMATIONS, backup_ha_file(AUTOMATIONS)))
        if script_changed:
            backups.append((SCRIPTS, backup_ha_file(SCRIPTS)))
        if config_changed:
            tmp = os.path.join(BASE_DIR, "config_entries.new")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            load_json(tmp)
            sudo_cp(tmp, CONFIG_ENTRIES)
        if auto_changed:
            tmp = os.path.join(BASE_DIR, "automations.new")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(new_auto)
            os.replace(tmp, AUTOMATIONS)
        if script_changed:
            tmp = os.path.join(BASE_DIR, "scripts.new")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(new_script)
            os.replace(tmp, SCRIPTS)
        if mem_changed:
            tmp = MEMORIES + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(new_memories, f, indent=2)
                f.write("\n")
            os.replace(tmp, MEMORIES)
        if loop_changed:
            with open(LOOP_BRIEF, "w", encoding="utf-8") as f:
                f.write(new_loop)
        restart_ha()
        ok = verify_window(5)
        if not ok or not ha_running():
            for orig, bak in backups:
                sudo_cp(bak, orig)
            restart_ha()
            ok2 = verify_window(5)
            if not ok2 or not ha_running():
                raise RuntimeError("HA verification failed after rollback")
            return {"ok": False, "rolled_back": True, "changed": [], "backups": [b for _, b in backups]}
        changed = []
        if config_changed:
            changed.append("config_entries")
        if auto_changed:
            changed.append("automations")
        if script_changed:
            changed.append("scripts")
        if mem_changed:
            changed.append("memories")
        if loop_changed:
            changed.append("loop_brief")
        return {"ok": True, "restarted": True, "changed": changed, "backups": [b for _, b in backups]}

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>EchoMuse Mercury</title>
<script>
(function(){try{var t=localStorage.getItem('pe-theme');if(!t){t=window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';}document.documentElement.setAttribute('data-theme',t);}catch(e){document.documentElement.setAttribute('data-theme','light');}})();
</script>
<style>
*{box-sizing:border-box}
:root{color-scheme:light}
[data-theme="light"]{--bg:#f6f5f2;--surface:#ffffff;--surface2:#f0efec;--text:#1c1917;--muted:#57534e;--border:#e7e5e4;--accent:#1d4ed8;--accent-soft:#dbeafe;--danger:#b91c1c;--ok:#15803d;--shadow:0 1px 2px rgba(0,0,0,.05),0 8px 24px rgba(0,0,0,.04)}
[data-theme="dark"]{--bg:#0c0a09;--surface:#1c1917;--surface2:#292524;--text:#f5f5f4;--muted:#a8a29e;--border:#44403c;--accent:#60a5fa;--accent-soft:#1e3a5f;--danger:#f87171;--ok:#4ade80;--shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.3)}
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text);margin:0;line-height:1.5}
.wrap{max-width:980px;margin:0 auto;padding:28px 20px 72px}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
h1{margin:0;font-size:30px;line-height:1.15;letter-spacing:-.02em}
.sub{color:var(--muted);font-size:14px;margin-top:4px}
.head-actions{display:flex;gap:8px;align-items:center}
button{font:inherit;cursor:pointer}
.btn{background:var(--accent);color:#fff;border:none;border-radius:10px;padding:10px 18px;font-size:15px;font-weight:600}
.btn:hover{filter:brightness(1.08)}
.btn:disabled{opacity:.55;cursor:default}
.btn-secondary{background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:10px 16px;font-size:15px;font-weight:600}
.btn-small{padding:6px 12px;font-size:13px;border-radius:8px}
.btn-danger{background:none;color:var(--danger);border:1px solid var(--border);border-radius:8px;padding:6px 12px;font-size:13px;font-weight:600}
a.logout{background:#d63031;color:#fff;text-decoration:none;padding:10px 16px;border-radius:10px;font-size:14px;font-weight:600}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--border);margin:24px 0 24px}
.tab{background:none;border:none;color:var(--muted);font-size:15px;font-weight:600;padding:11px 18px;border-radius:10px 10px 0 0;cursor:pointer}
.tab.active{color:var(--text);background:var(--surface);border:1px solid var(--border);border-bottom:1px solid var(--surface);margin-bottom:-1px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:var(--shadow)}
.card h2{margin:0 0 6px;font-size:17px;letter-spacing:-.01em}
.card .hint{color:var(--muted);font-size:13.5px;margin:0 0 12px}
label{display:block;font-weight:600;font-size:13px;margin:16px 0 6px}
label:first-child{margin-top:0}
textarea,input[type=text],input[type=search],input[type=password],select{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:10px 12px;font:inherit;font-size:15px}
textarea{resize:vertical}
input:focus,textarea:focus,select:focus{outline:2px solid var(--accent);outline-offset:1px}
small{color:var(--muted)}
.notice{background:var(--accent-soft);border:1px solid var(--border);border-radius:12px;padding:12px 16px;font-size:14px;margin-bottom:16px}
#err{color:var(--danger);font-weight:600}
#status,#cmdstatus{margin-top:16px;font-weight:600}
.mem{border:1px solid var(--border);border-radius:10px;padding:10px;margin-top:8px;display:flex;gap:8px;flex-wrap:wrap}
.mem input{flex:1;min-width:180px}
.cmd{margin-bottom:16px}
.cmd-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:4px}
.cmd-label{font-size:17px;font-weight:700;border:none;background:none;padding:0;width:auto;flex:1;min-width:180px;color:var(--text)}
.cmd-label:focus{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
.badge{font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px;white-space:nowrap}
.badge-do{background:var(--accent-soft);color:var(--accent)}
.badge-ask{background:#dcfce7;color:#166534}
[data-theme="dark"] .badge-ask{background:#14532d;color:#86efac}
.badge-builtin{background:var(--surface2);color:var(--muted);border:1px solid var(--border)}
.entity{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;color:var(--muted);background:var(--surface2);padding:2px 8px;border-radius:6px;word-break:break-all}
.chips{margin:8px 0}
.chip{display:inline-flex;gap:6px;align-items:center;background:var(--surface2);border:1px solid var(--border);border-radius:999px;padding:5px 6px 5px 12px;font-size:13.5px;margin:0 6px 6px 0}
.chip.pat{border-style:dashed}
.chip button{border:none;background:none;color:var(--muted);font-size:15px;padding:0 4px;line-height:1}
.chip button:hover{color:var(--danger)}
.addrow{display:flex;gap:8px;margin-top:8px}
.addrow input{flex:1}
.callrow,.entrow{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
.callrow input,.entrow input{flex:1;min-width:140px}
.callrow .databox{min-width:180px}
.testrow{display:flex;gap:10px;flex-wrap:wrap}
.testrow input{flex:1;min-width:220px}
.testresult{margin-top:12px;padding:12px 14px;border-radius:10px;background:var(--surface2);font-size:14px}
.testresult .ok{color:var(--ok);font-weight:700}
.testresult .no{color:var(--muted);font-weight:700}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
.toolbar input{flex:1;min-width:220px}
.stats{color:var(--muted);font-size:13.5px;margin-bottom:16px}
@media (max-width:640px){.wrap{padding:20px 12px 56px}h1{font-size:26px}.card{padding:16px}}
</style>
</head><body>
<div class="wrap">
<header>
<div><h1>EchoMuse Mercury</h1><div class="sub">Mercury prompts, voice commands, and voice settings. One console for the EchoMuse Mercury project.</div></div>
<div class="head-actions">
<button id="themebtn" class="btn-secondary btn-small" type="button">Dark mode</button>
<a class="logout" href="https://auth.petrafied.net/if/flow/default-invalidation-flow/">Log out</a>
</div>
</header>
<div class="tabs" role="tablist">
<button class="tab active" id="tabbtn-prompts" type="button" onclick="showTab('prompts')">Prompts</button>
<button class="tab" id="tabbtn-commands" type="button" onclick="showTab('commands')">Commands</button>
<button class="tab" id="tabbtn-settings" type="button">Settings</button>
</div>
<div id="tab-prompts">
<div class="notice">Edit Mercury's prompt, function descriptions, automation and script spoken lines and sounds, memories, and the ask-Muse loop brief. Hit Save to apply. Saving restarts Home Assistant (about 35 seconds) and then just works. Do not close this page while saving.</div>
<div id="err"></div>
<div class="card"><label>Mercury prompt</label><textarea id="prompt" rows="18"></textarea></div>
<div class="card"><div id="funcs"></div></div>
<div class="card"><label>Ollama agent prompt</label><textarea id="ollama" rows="4"></textarea></div>
<div class="card"><h2>Automation lines and sounds</h2>
<label>Alarm sound (60s ding)</label><input type="text" id="line_alarm" readonly>
<label>Office timer sound (60s ding)</label><input type="text" id="line_timer" readonly>
<label>Frigate narration (template)</label><input type="text" id="line_frigate">
<label>Reminder (template)</label><input type="text" id="line_reminder">
</div>
<div class="card"><h2>Script spoken lines</h2>
<label>Goodnight</label><input type="text" id="line_goodnight">
<label>Movie night</label><input type="text" id="line_movie">
<label>Morning briefing (template)</label><textarea id="line_morning" rows="8"></textarea>
</div>
<div class="card"><h2>Memories (remember / recall)</h2><div id="mems"></div><button id="addmem" class="btn-secondary btn-small" type="button">Add memory</button></div>
<div class="card"><h2>Ask-Muse loop brief</h2><p class="hint">Edits here take effect on the next voice-loop run (the loop agent reads this file; if unreachable it uses its built-in brief).</p><textarea id="loopbrief" rows="14"></textarea></div>
<button id="save" class="btn" type="button">Save (restarts Home Assistant)</button>
<button id="reload" class="btn-secondary" type="button">Reload (discard unsaved edits)</button>
<div id="status"></div>
</div>
<div id="tab-commands" style="display:none">
<div class="notice">Voice commands the Dot answers without Mercury. Say a listed phrase and it runs at once: actions ding, questions answer out loud. Anything not listed still goes to Decide, then Mercury 3. Saving commands does not restart Home Assistant; the voice service picks them up on its own.</div>
<div class="card">
<div class="toolbar">
<input type="search" id="cmdsearch" placeholder="Search commands, devices, phrases...">
<button id="cmdadd" class="btn-secondary" type="button">Add command</button>
<button id="cmdsave" class="btn" type="button">Save commands</button>
</div>
<div class="stats" id="cmdstats"></div>
<div class="testrow">
<input type="text" id="cmdtest" placeholder="Test a phrase, for example: turn the lights on">
<button id="cmdtestbtn" class="btn-secondary" type="button">Test</button>
</div>
<div class="testresult" id="cmdtestresult">Type a phrase and press Test to see exactly what it would do.</div>
<div id="cmdstatus"></div>
</div>
<div id="cmdlist"></div>
<div class="card"><h2>Built in</h2><p class="hint">These run before the matcher and are not editable here. Source: STOP_PHRASES and TIMER_CANCEL_PHRASES in the voice service.</p>
<div style="margin-bottom:12px"><span class="badge badge-builtin">BUILT IN</span> <strong>Stop</strong> <span class="entity">stops all playback, silently</span><div class="chips"><span class="chip">stop</span><span class="chip">be quiet</span><span class="chip">shut up</span><span class="chip">shut the fuck up</span><span class="chip">quiet</span><span class="chip">silence</span><span class="chip">stop that</span><span class="chip">stop the noise</span><span class="chip">turn it off</span><span class="chip">stop the alarm</span><span class="chip">stop the timer</span><span class="chip">stop playing</span><span class="chip">stop the sound</span></div></div>
<div style="margin-bottom:12px"><span class="badge badge-builtin">BUILT IN</span> <strong>Timer cancel</strong> <span class="entity">cancels the office timer</span><div class="chips"><span class="chip">stop the timer</span><span class="chip">cancel the timer</span><span class="chip">stop my timer</span><span class="chip">cancel my timer</span><span class="chip">stop the office timer</span><span class="chip">cancel the office timer</span></div></div>
<div><span class="badge badge-builtin">BUILT IN</span> <strong>Ask Muse</strong> <span class="entity">relays the question to Muse</span><div class="chips"><span class="chip">ask Muse ...</span><span class="chip">tell Muse ...</span></div></div>
</div>
</div>
<div id="tab-settings" style="display:none">
<div class="notice">Voice service settings, live from Mercury Engine on port 8896. Keys are stored only in engine_config.json on thinclient, never shown back. Saving keys or voice here does not restart Home Assistant and does not touch the Dot voice path.</div>
<div class="card"><h2>Status</h2><div id="engineTiles"></div><div style="margin-top:12px"><button id="engineRefresh" class="btn-secondary btn-small" type="button">Refresh status</button> <span id="engineUpdated" style="color:var(--muted);font-size:13.5px"></span></div></div>
<div class="card"><h2>Keys and voice</h2><p class="hint">Fill in a field to replace that key. A field left showing dots keeps the stored key.</p>
<label>Inception API key</label><input type="password" id="engine_inception_key" autocomplete="off"><span id="engine_inception_key_state" style="color:var(--muted);font-size:13.5px"></span>
<label>Home Assistant long-lived access token</label><input type="password" id="engine_ha_token" autocomplete="off"><span id="engine_ha_token_state" style="color:var(--muted);font-size:13.5px"></span>
<label>Google Cloud API key</label><input type="password" id="engine_google_api_key" autocomplete="off"><span id="engine_google_api_key_state" style="color:var(--muted);font-size:13.5px"></span>
<label>Groq API key</label><input type="password" id="engine_groq_api_key" autocomplete="off"><span id="engine_groq_api_key_state" style="color:var(--muted);font-size:13.5px"></span>
<label>Voice</label><select id="engine_voice_choice"><option value="piper">Piper (local, free)</option><option value="google">Google WaveNet (free tier)</option></select>
<label>Google WaveNet voice</label><select id="engine_google_voice_name"><option value="en-US-Wavenet-A">en-US-Wavenet-A (Male)</option><option value="en-US-Wavenet-B">en-US-Wavenet-B (Male)</option><option value="en-US-Wavenet-C">en-US-Wavenet-C (Female)</option><option value="en-US-Wavenet-D">en-US-Wavenet-D (Male)</option><option value="en-US-Wavenet-E">en-US-Wavenet-E (Female)</option><option value="en-US-Wavenet-F">en-US-Wavenet-F (Female)</option><option value="en-US-Wavenet-G">en-US-Wavenet-G (Female)</option><option value="en-US-Wavenet-H">en-US-Wavenet-H (Female)</option><option value="en-US-Wavenet-I">en-US-Wavenet-I (Male)</option><option value="en-US-Wavenet-J">en-US-Wavenet-J (Male)</option></select>
<div style="margin-top:12px"><button id="engineSaveKeys" class="btn-secondary" type="button">Save keys and voice</button></div><div id="engineKeyResult" style="margin-top:12px;font-weight:600"></div></div>
<div class="card"><h2>Voice test (browser only)</h2><p class="hint">Audio plays in this browser only, never on the Dot.</p><label>Test phrase</label><input type="text" id="engineTestPhrase" value="Hello, this is your Mercury voice assistant."><div style="margin-top:12px"><button id="engineTestBtn" class="btn-secondary" type="button">Test voice</button></div><div id="engineTestResult" style="margin-top:12px"></div><audio id="engineTestPlayer" controls style="width:100%;margin-top:12px"></audio></div>
<div class="card"><h2>Talk test (browser only)</h2><p class="hint">Hold to talk, or type a question. This uses the same Decide and Mercury path as typed questions, and speaks the reply in this browser. It does not use the Dot.</p><div><button id="engineTalkBtn" class="btn-secondary" type="button">Hold to talk</button> <button id="engineStopBtn" class="btn-secondary" type="button">Stop</button></div><div style="margin-top:12px;display:flex;gap:8px"><input type="text" id="engineTyped" placeholder="Type a question or command"><button id="engineSendTyped" class="btn-secondary" type="button">Send</button></div><div id="engineTalkResult" style="margin-top:12px;font-weight:600"></div><div id="engineLog" style="white-space:pre-wrap;background:var(--surface2);padding:12px;border-radius:8px;min-height:6em;font-family:ui-monospace,Menlo,monospace;font-size:13px;margin-top:12px"></div><audio id="enginePlayer" controls style="width:100%;margin-top:12px"></audio></div>
</div>
</div>
<script>
function el(id){return document.getElementById(id);}
var mems=[];
function renderMems(){
  var box=el('mems'); box.innerHTML='';
  mems.forEach(function(m,i){
    var d=document.createElement('div'); d.className='mem';
    var t=document.createElement('input'); t.type='text'; t.placeholder='Memory text'; t.value=m.text||'';
    t.oninput=function(){mems[i].text=t.value;};
    var tp=document.createElement('input'); tp.type='text'; tp.placeholder='Topic'; tp.value=m.topic||'';
    tp.oninput=function(){mems[i].topic=tp.value;};
    var del=document.createElement('button'); del.type='button'; del.textContent='Delete'; del.className='btn-danger';
    del.onclick=function(){mems.splice(i,1); renderMems();};
    d.appendChild(t); d.appendChild(tp); d.appendChild(del); box.appendChild(d);
  });
}
function load(){
  el('status').textContent='Loading...';
  fetch('/api/prompts').then(function(r){return r.json();}).then(function(d){
    if(d.error){el('err').textContent=d.error; return;}
    el('prompt').value=d.prompt||'';
    el('ollama').value=d.ollama_prompt||'';
    var fbox=el('funcs'); fbox.innerHTML='<h2>Function descriptions</h2>';
    (d.functions||[]).forEach(function(fn){
      var lab=document.createElement('label'); lab.textContent=fn.name;
      var ta=document.createElement('textarea'); ta.rows=3; ta.value=fn.description||''; ta.dataset.fn=fn.name;
      fbox.appendChild(lab); fbox.appendChild(ta);
    });
    var L=d.lines||{};
    el('line_alarm').value=L.alarm||'';
    el('line_timer').value=L.timer||'';
    el('line_frigate').value=L.frigate||'';
    el('line_reminder').value=L.reminder||'';
    el('line_goodnight').value=L.goodnight||'';
    el('line_movie').value=L.movie||'';
    el('line_morning').value=L.morning||'';
    mems=(d.memories||[]).map(function(m){return {id:m.id||'', text:m.text||'', topic:m.topic||'', created_at:m.created_at||''};});
    renderMems();
    el('loopbrief').value=d.loop_brief||'';
    el('status').textContent='Loaded. Prompt length: '+(d.prompt_len||0)+' characters.';
  }).catch(function(e){el('err').textContent='Load failed: '+e;});
}
el('addmem').onclick=function(){
  var now=new Date().toISOString();
  mems.push({id:'ed-'+Date.now()+'-'+mems.length, text:'', topic:'', created_at:now});
  renderMems();
};
el('reload').onclick=function(){load();};
el('save').onclick=function(){
  if(el('prompt').value.trim()===''){el('status').textContent='Prompt must not be empty. Reload first.'; return;}
  var lineIds=['frigate','reminder','goodnight','movie','morning'];
  for(var k=0;k<lineIds.length;k++){
    var v=el('line_'+lineIds[k]).value;
    if(v.trim()===''){el('status').textContent='Spoken line '+lineIds[k]+' must not be empty.'; return;}
  }
  var funcs={};
  var tas=document.querySelectorAll('#funcs textarea');
  tas.forEach(function(ta){funcs[ta.dataset.fn]=ta.value;});
  var cleanMems=mems.filter(function(m){return (m.text||'').trim()!=='';});
  var payload={
    prompt: el('prompt').value,
    functions: funcs,
    ollama_prompt: el('ollama').value,
    lines: {
      frigate: el('line_frigate').value,
      reminder: el('line_reminder').value,
      goodnight: el('line_goodnight').value,
      movie: el('line_movie').value,
      morning: el('line_morning').value
    },
    memories: cleanMems,
    loop_brief: el('loopbrief').value
  };
  el('status').textContent='Saving... Home Assistant is restarting (about 40-60 seconds). Do not close this page.';
  el('save').disabled=true;
  fetch('/api/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)}).then(function(r){return r.json();}).then(function(d){
    el('save').disabled=false;
    if(d.ok){el('status').textContent='Saved. Changed: '+(d.changed||[]).join(', ')+' Backups: '+(d.backups||[]).join(', ');}
    else if(d.rolled_back){el('status').textContent='Save failed verification and was rolled back. Backups: '+(d.backups||[]).join(', ');}
    else {el('status').textContent='Save failed: '+(d.error||'unknown error');}
  }).catch(function(e){el('save').disabled=false; el('status').textContent='Save request failed: '+e;});
};
load();
function showTab(name){
  el('tab-prompts').style.display = (name === 'prompts') ? '' : 'none';
  el('tab-commands').style.display = (name === 'commands') ? '' : 'none';
  el('tabbtn-prompts').className = 'tab' + (name === 'prompts' ? ' active' : '');
  el('tabbtn-commands').className = 'tab' + (name === 'commands' ? ' active' : '');
}
function showTab(name){
  el("tab-prompts").style.display = (name === "prompts") ? "" : "none";
  el("tab-commands").style.display = (name === "commands") ? "" : "none";
  el("tab-settings").style.display = (name === "settings") ? "" : "none";
  el("tabbtn-prompts").className = "tab" + (name === "prompts" ? " active" : "");
  el("tabbtn-commands").className = "tab" + (name === "commands" ? " active" : "");
  el("tabbtn-settings").className = "tab" + (name === "settings" ? " active" : "");
  if (name === "settings") { engineRefresh(); }
}
el("tabbtn-settings").onclick = function(){ showTab("settings"); };
var ENGINE_MASKED = "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022";
function engineLog(m){ el("engineLog").textContent += m + String.fromCharCode(10); }
function engineTile(label, value){
  var d = document.createElement("div");
  d.textContent = label + ": " + value;
  return d;
}
function engineRenderTiles(j){
  var c = el("engineTiles"); c.innerHTML = "";
  c.appendChild(engineTile("Service", j.ok ? "Healthy" : "Down"));
  c.appendChild(engineTile("Whisper", j.whisper_ready ? "Ready" : "Not ready"));
  c.appendChild(engineTile("Piper", j.piper_ready ? "Ready" : "Not ready"));
  c.appendChild(engineTile("Piper voice", j.voice || "none"));
  c.appendChild(engineTile("Inception key", j.inception_key_set ? "Set" : "Not set"));
  c.appendChild(engineTile("HA token", j.ha_token_set ? "Set" : "Not set"));
  c.appendChild(engineTile("Google key", j.google_api_key_set ? "Set" : "Not set"));
  c.appendChild(engineTile("Groq key", j.groq_api_key_set ? "Set" : "Not set"));
  var voice = (j.voice_choice === "google") ? ("Google WaveNet - " + (j.google_voice_name || "en-US-Wavenet-D")) : "Piper (local)";
  c.appendChild(engineTile("Voice", voice));
  el("engineUpdated").textContent = "Updated " + new Date().toLocaleTimeString();
}
function engineApplyKeys(j){
  var fields = [["engine_inception_key", "inception_key_set"], ["engine_ha_token", "ha_token_set"], ["engine_google_api_key", "google_api_key_set"], ["engine_groq_api_key", "groq_api_key_set"]];
  fields.forEach(function(pair){
    var input = el(pair[0]);
    var state = el(pair[0] + "_state");
    if (j[pair[1]]) { input.value = ENGINE_MASKED; input.dataset.masked = "true"; state.textContent = "Set"; }
    else { input.value = ""; input.dataset.masked = ""; state.textContent = "Not set"; }
  });
  el("engine_voice_choice").value = j.voice_choice || "piper";
  el("engine_google_voice_name").value = j.google_voice_name || "en-US-Wavenet-D";
  el("engine_google_voice_name").disabled = (el("engine_voice_choice").value !== "google");
}
function engineUnmask(input){
  if (input.dataset.masked === "true") { input.value = ""; input.dataset.masked = ""; }
}
["engine_inception_key", "engine_ha_token", "engine_google_api_key", "engine_groq_api_key"].forEach(function(id){
  el(id).addEventListener("focus", function(){ engineUnmask(el(id)); });
  el(id).addEventListener("input", function(){ engineUnmask(el(id)); });
});
el("engine_voice_choice").addEventListener("change", function(){ el("engine_google_voice_name").disabled = (el("engine_voice_choice").value !== "google"); });
async function engineRefresh(){
  try {
    var r = await fetch("/api/engine-status");
    var j = await r.json();
    engineRenderTiles(j);
    engineApplyKeys(j);
  } catch (e) {
    el("engineTiles").textContent = "Voice service unreachable.";
  }
}
el("engineRefresh").onclick = engineRefresh;
async function engineSaveKeys(){
  var body = {};
  var fields = [["engine_inception_key", "inception_key"], ["engine_ha_token", "ha_token"], ["engine_google_api_key", "google_api_key"], ["engine_groq_api_key", "groq_api_key"]];
  fields.forEach(function(pair){
    var input = el(pair[0]);
    if (input.dataset.masked !== "true" && input.value) { body[pair[1]] = input.value; }
  });
  body.voice_choice = el("engine_voice_choice").value;
  body.google_voice_name = el("engine_google_voice_name").value;
  var r = await fetch("/api/engine-config", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  var j = await r.json();
  var fmt = function(b){ return b ? "Set" : "Not set"; };
  el("engineKeyResult").textContent = "Saved. Inception: " + fmt(j.inception_key_set) + ", HA token: " + fmt(j.ha_token_set) + ", Google: " + fmt(j.google_api_key_set) + ", Groq: " + fmt(j.groq_api_key_set) + ", voice: " + j.voice_choice;
  engineRefresh();
}
el("engineSaveKeys").onclick = engineSaveKeys;
async function engineTestVoice(){
  var res = el("engineTestResult");
  var phrase = el("engineTestPhrase").value.trim() || "Hello, this is your Mercury voice assistant.";
  try {
    res.textContent = "Saving voice selection...";
    await fetch("/api/engine-config", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({voice_choice: el("engine_voice_choice").value, google_voice_name: el("engine_google_voice_name").value})});
    res.textContent = "Synthesizing...";
    var r = await fetch("/api/engine-tts", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({text: phrase})});
    if (!r.ok) { res.textContent = "Test failed (TTS error)."; return; }
    var blob = await r.blob();
    var url = URL.createObjectURL(blob);
    var player = el("engineTestPlayer"); player.src = url; player.play();
    var via = ((r.headers.get("Content-Type") || "").indexOf("mpeg") >= 0) ? "Google WaveNet" : "Piper (local)";
    res.textContent = "Playing via " + via + ".";
  } catch (e) { res.textContent = "Test failed: " + e; }
}
el("engineTestBtn").onclick = engineTestVoice;
var engineRec = null;
var engineChunks = [];
async function engineStartRec(e){
  if (e) { e.preventDefault(); }
  engineChunks = [];
  try {
    var stream = await navigator.mediaDevices.getUserMedia({audio: true});
    engineRec = new MediaRecorder(stream);
    engineRec.ondataavailable = function(ev){ if (ev.data.size) { engineChunks.push(ev.data); } };
    engineRec.onstop = async function(){
      var blob = new Blob(engineChunks, {type: engineRec.mimeType || "audio/webm"});
      engineLog("Recorded " + blob.size + " bytes, transcribing...");
      var r = await fetch("/api/engine-transcribe", {method: "POST", body: blob});
      var j = await r.json();
      if (j.text) { engineLog("Heard: " + j.text); engineHandleAsk(j.text); }
      else { engineLog("Transcription failed: " + (j.error || "unknown")); }
      stream.getTracks().forEach(function(t){ t.stop(); });
    };
    engineRec.start();
    engineLog("Listening... release the button to send.");
  } catch (err) { engineLog("Mic error: " + err); }
}
function engineStopRec(e){
  if (e) { e.preventDefault(); }
  if (engineRec && engineRec.state !== "inactive") { engineRec.stop(); }
}
el("engineTalkBtn").addEventListener("mousedown", engineStartRec);
el("engineTalkBtn").addEventListener("mouseup", engineStopRec);
el("engineTalkBtn").addEventListener("touchstart", engineStartRec);
el("engineTalkBtn").addEventListener("touchend", engineStopRec);
async function engineHandleAsk(text){
  var r = await fetch("/api/engine-ask", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({text: text})});
  var j = await r.json();
  var route = j.route ? (j.route.route + " (" + j.route.source + ")") : "unknown";
  el("engineTalkResult").textContent = "Action: " + (j.action || j.error || "unknown") + " - Route: " + route;
  if (j.action === "stop") { engineLog("STOP action (silent)."); return; }
  if (j.reply_text) { engineLog("Reply: " + j.reply_text); engineSpeak(j.reply_text); }
  else { engineLog("No reply text (see result above)."); }
}
async function engineSpeak(text){
  var r = await fetch("/api/engine-tts", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({text: text})});
  if (!r.ok) { engineLog("TTS failed."); return; }
  var blob = await r.blob();
  var url = URL.createObjectURL(blob);
  var player = el("enginePlayer"); player.src = url; player.play();
}
async function engineDoStop(){
  var p = el("enginePlayer"); p.pause(); p.currentTime = 0;
  var tp = el("engineTestPlayer"); tp.pause(); tp.currentTime = 0;
  var r = await fetch("/api/engine-stop", {method: "POST"});
  var j = await r.json();
  engineLog("STOP: server stopped: " + (j.stopped ? "yes" : "no") + ".");
}
el("engineStopBtn").onclick = engineDoStop;
el("engineSendTyped").onclick = function(){
  var t = el("engineTyped").value.trim();
  if (t) { engineHandleAsk(t); }
};
function themeLabel(){
  var t = document.documentElement.getAttribute('data-theme') || 'light';
  el('themebtn').textContent = (t === 'dark') ? 'Light mode' : 'Dark mode';
}
el('themebtn').onclick = function(){
  var cur = document.documentElement.getAttribute('data-theme') || 'light';
  var next = (cur === 'dark') ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('pe-theme', next); } catch (e) {}
  themeLabel();
};
themeLabel();
var cmds = [];
var STOP_SET = ['stop','shut up','be quiet','quiet','silence','stop that','stop the noise','turn it off','stop the alarm','stop the timer','stop playing','stop the sound','shut the fuck up'];
var TIMER_SET = ['stop the timer','cancel the timer','stop my timer','cancel my timer','stop the office timer','cancel the office timer'];
function cmdNorm(s){ return (s || '').toLowerCase().replace(/\\s+/g, ' ').replace(/[.?!]+$/g, '').trim(); }
function esc(s){ var d = document.createElement('div'); d.textContent = (s === undefined || s === null) ? '' : String(s); return d.innerHTML; }
function slugify(s){
  var out = (s || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  return out;
}
function callServiceText(c){ return c.service || ''; }
function renderCmds(){
  var q = cmdNorm(el('cmdsearch').value);
  var box = el('cmdlist'); box.innerHTML = '';
  var totalP = 0, totalPat = 0;
  cmds.forEach(function(c, i){
    totalP += (c.phrases || []).length; totalPat += (c.patterns || []).length;
  });
  el('cmdstats').textContent = cmds.length + ' commands, ' + totalP + ' phrases, ' + totalPat + ' patterns. Phrases match exactly; patterns fill in a duration, message, camera, or time.';
  cmds.forEach(function(c, i){
    var hay = cmdNorm([c.label, c.device, (c.phrases || []).join(' '), (c.patterns || []).join(' ')].join(' '));
    if (q && hay.indexOf(q) < 0) { return; }
    var d = document.createElement('div'); d.className = 'card cmd'; d.id = 'cmdcard-' + i;
    var h = '<div class="cmd-head">';
    h += '<input class="cmd-label" data-ci="' + i + '" data-f="label" value="' + esc(c.label) + '" placeholder="Command name">';
    h += '<select data-ci="' + i + '" data-f="kind" class="btn-secondary btn-small" style="width:auto"><option value="do"' + (c.kind === 'do' ? ' selected' : '') + '>Do</option><option value="ask"' + (c.kind === 'ask' ? ' selected' : '') + '>Ask</option></select>';
    h += '<button class="btn-danger" type="button" data-action="del-cmd" data-ci="' + i + '">Delete</button></div>';
    h += '<label>Device</label><input type="text" data-ci="' + i + '" data-f="device" value="' + esc(c.device) + '" placeholder="Lights, Lamp, Vacuum...">';
    if (c.kind === 'do') {
      h += '<label>Calls (service, device, data as JSON)</label>';
      (c.calls || []).forEach(function(call, j){
        h += '<div class="callrow">';
        h += '<input type="text" data-ci="' + i + '" data-call="' + j + '" data-cf="service" value="' + esc(call.service) + '" placeholder="light.turn_on">';
        h += '<input type="text" data-ci="' + i + '" data-call="' + j + '" data-cf="entity" value="' + esc(call.entity) + '" placeholder="light.lights">';
        h += '<input type="text" class="databox" data-ci="' + i + '" data-call="' + j + '" data-cf="data" value="' + esc(call.data) + '" placeholder="{&quot;sound&quot;: &quot;pink&quot;}">';
        h += '<button class="btn-danger" type="button" data-action="del-call" data-ci="' + i + '" data-j="' + j + '">Remove</button></div>';
      });
      h += '<div class="addrow"><button class="btn-secondary btn-small" type="button" data-action="add-call" data-ci="' + i + '">Add call</button></div>';
    } else {
      if (c.source === 'clock') {
        h += '<p class="hint">Source: clock (no device needed).</p>';
        h += '<div class="addrow"><button class="btn-secondary btn-small" type="button" data-action="use-device" data-ci="' + i + '">Use a device instead</button></div>';
      } else {
        h += '<label>Devices (name, device)</label>';
        (c.entities || []).forEach(function(en, j){
          h += '<div class="entrow">';
          h += '<input type="text" data-ci="' + i + '" data-ent="' + j + '" data-ef="key" value="' + esc(en.key) + '" placeholder="name">';
          h += '<input type="text" data-ci="' + i + '" data-ent="' + j + '" data-ef="entity" value="' + esc(en.entity) + '" placeholder="weather.openweathermap">';
          h += '<button class="btn-danger" type="button" data-action="del-ent" data-ci="' + i + '" data-j="' + j + '">Remove</button></div>';
        });
        h += '<div class="addrow"><button class="btn-secondary btn-small" type="button" data-action="add-ent" data-ci="' + i + '">Add device</button>';
        h += '<button class="btn-secondary btn-small" type="button" data-action="use-clock" data-ci="' + i + '">Use the clock instead</button></div>';
      }
      h += '<label>Answer (use {state}, {attr:name}, {name}, {name:attr}, {name:remaining})</label>';
      h += '<textarea rows="2" data-ci="' + i + '" data-f="answer">' + esc(c.answer) + '</textarea>';
    }
    h += '<label>Phrases (exact matches)</label><div class="chips">';
    (c.phrases || []).forEach(function(p, j){
      h += '<span class="chip">' + esc(p) + '<button type="button" data-action="del-phrase" data-ci="' + i + '" data-j="' + j + '">x</button></span>';
    });
    h += '</div><div class="addrow"><input type="text" data-ci="' + i + '" data-add="phrase" placeholder="Add a phrase"><button class="btn-secondary btn-small" type="button" data-action="add-phrase" data-ci="' + i + '">Add phrase</button></div>';
    h += '<label>Patterns (with {minutes}, {message}, {camera}, or {time})</label><div class="chips">';
    (c.patterns || []).forEach(function(p, j){
      h += '<span class="chip pat" title="Pattern: fills in the bracketed value">' + esc(p) + '<button type="button" data-action="del-pattern" data-ci="' + i + '" data-j="' + j + '">x</button></span>';
    });
    h += '</div><div class="addrow"><input type="text" data-ci="' + i + '" data-add="pattern" placeholder="Add a pattern"><button class="btn-secondary btn-small" type="button" data-action="add-pattern" data-ci="' + i + '">Add pattern</button></div>';
    d.innerHTML = h;
    box.appendChild(d);
  });
}
el('cmdlist').addEventListener('input', function(e){
  var t = e.target; var ci = t.getAttribute('data-ci'); if (ci === null) { return; }
  ci = parseInt(ci, 10); var c = cmds[ci]; if (!c) { return; }
  var f = t.getAttribute('data-f');
  if (f === 'label') { c.label = t.value; }
  else if (f === 'device') { c.device = t.value; }
  else if (f === 'answer') { c.answer = t.value; }
  var call = t.getAttribute('data-call');
  if (call !== null) { var cc = c.calls[parseInt(call, 10)]; if (cc) { cc[t.getAttribute('data-cf')] = t.value; } }
  var en = t.getAttribute('data-ent');
  if (en !== null) { var ee = c.entities[parseInt(en, 10)]; if (ee) { ee[t.getAttribute('data-ef')] = t.value; } }
});
el('cmdlist').addEventListener('change', function(e){
  var t = e.target; var ci = t.getAttribute('data-ci'); if (ci === null) { return; }
  ci = parseInt(ci, 10); var c = cmds[ci]; if (!c) { return; }
  if (t.getAttribute('data-f') === 'kind') {
    c.kind = t.value;
    if (c.kind === 'do' && (!c.calls || c.calls.length === 0)) { c.calls = [{service:'', entity:'', data:''}]; }
    if (c.kind === 'ask' && (!c.entities || c.entities.length === 0) && c.source !== 'clock') { c.entities = [{key:'', entity:''}]; }
    renderCmds();
  }
});
el('cmdlist').addEventListener('click', function(e){
  var t = e.target; var act = t.getAttribute('data-action'); if (!act) { return; }
  var ci = parseInt(t.getAttribute('data-ci'), 10); var c = cmds[ci]; if (!c) { return; }
  if (act === 'del-cmd') { cmds.splice(ci, 1); renderCmds(); return; }
  var j = t.getAttribute('data-j');
  if (act === 'del-phrase') { c.phrases.splice(parseInt(j, 10), 1); renderCmds(); return; }
  if (act === 'del-pattern') { c.patterns.splice(parseInt(j, 10), 1); renderCmds(); return; }
  if (act === 'del-call') { c.calls.splice(parseInt(j, 10), 1); renderCmds(); return; }
  if (act === 'del-ent') { c.entities.splice(parseInt(j, 10), 1); renderCmds(); return; }
  if (act === 'add-call') { c.calls.push({service:'', entity:'', data:''}); renderCmds(); return; }
  if (act === 'add-ent') { c.entities.push({key:'', entity:''}); renderCmds(); return; }
  if (act === 'use-clock') { c.source = 'clock'; renderCmds(); return; }
  if (act === 'use-device') { c.source = undefined; if (!c.entities || c.entities.length === 0) { c.entities = [{key:'', entity:''}]; } renderCmds(); return; }
  if (act === 'add-phrase' || act === 'add-pattern') {
    var inp = document.querySelector('input[data-ci="' + ci + '"][data-add="' + (act === 'add-phrase' ? 'phrase' : 'pattern') + '"]');
    if (inp && inp.value.trim() !== '') {
      if (act === 'add-phrase') { c.phrases.push(inp.value.trim()); } else { c.patterns.push(inp.value.trim()); }
      renderCmds();
    }
    return;
  }
});
el('cmdsearch').addEventListener('input', function(){ renderCmds(); });
el('cmdadd').onclick = function(){
  cmds.push({id:'', label:'', kind:'do', device:'', phrases:[], patterns:[], calls:[{service:'', entity:'', data:''}], entities:[], answer:'', _new:true});
  renderCmds();
  var last = document.getElementById('cmdcard-' + (cmds.length - 1));
  if (last && last.scrollIntoView) { last.scrollIntoView(); }
};
function gatherCmds(){
  var out = []; var seenIds = {}; var seenPhrases = {};
  cmds.forEach(function(c, i){
    var label = (c.label || '').trim();
    if (label === '') { throw new Error('Command ' + (i + 1) + ' needs a name.'); }
    var id = (c.id || '').trim();
    if (id === '') { id = slugify(label) || ('cmd' + (i + 1)); }
    var base = id; var n = 2;
    while (seenIds[id]) { id = base + '_' + n; n++; }
    seenIds[id] = true;
    var phrases = (c.phrases || []).map(function(p){ return p.trim(); }).filter(function(p){ return p !== ''; });
    var patterns = (c.patterns || []).map(function(p){ return p.trim(); }).filter(function(p){ return p !== ''; });
    if (phrases.length + patterns.length === 0) { throw new Error('Command "' + label + '" needs at least one phrase or pattern.'); }
    phrases.forEach(function(p){
      var norm = cmdNorm(p);
      if (STOP_SET.indexOf(norm) >= 0 || TIMER_SET.indexOf(norm) >= 0) { throw new Error('Phrase "' + p + '" in "' + label + '" is built in and cannot be a command phrase.'); }
      if (norm.indexOf('ask muse') === 0 || norm.indexOf('tell muse') === 0) { throw new Error('Phrase "' + p + '" in "' + label + '" starts with Ask Muse and cannot be a command phrase.'); }
      if (seenPhrases[norm]) { throw new Error('Phrase "' + p + '" is used by both "' + seenPhrases[norm] + '" and "' + label + '".'); }
      seenPhrases[norm] = label;
    });
    var cmd = {id:id, label:label, kind:c.kind, device:(c.device || '').trim(), phrases:phrases, patterns:patterns};
    if (c.kind === 'do') {
      var calls = [];
      (c.calls || []).forEach(function(call){
        var svc = (call.service || '').trim(); var ent = (call.entity || '').trim();
        if (svc === '' || ent === '') { throw new Error('Command "' + label + '" has a call missing a service or device.'); }
        var parts = svc.split('.');
        if (parts.length !== 2 || parts[0] === '' || parts[1] === '') { throw new Error('Service "' + svc + '" in "' + label + '" must look like domain.service.'); }
        var data = {};
        if ((call.data || '').trim() !== '') {
          try { data = JSON.parse(call.data); } catch (e) { throw new Error('Service data in "' + label + '" is not valid JSON.'); }
          if (!data || typeof data !== 'object' || Array.isArray(data)) { throw new Error('Service data in "' + label + '" must be a JSON object.'); }
        }
        calls.push({domain:parts[0], service:parts[1], entity_id:ent, data:data});
      });
      if (calls.length === 0) { throw new Error('Command "' + label + '" needs at least one call.'); }
      cmd.calls = calls;
    } else {
      var answer = (c.answer || '').trim();
      if (answer === '') { throw new Error('Command "' + label + '" needs an answer.'); }
      cmd.answer = answer;
      if (c.source === 'clock') { cmd.source = 'clock'; }
      else {
        var ents = {};
        (c.entities || []).forEach(function(en){
          var k = (en.key || '').trim(); var v = (en.entity || '').trim();
          if (k === '' || v === '') { throw new Error('Command "' + label + '" has a device row missing a name or device.'); }
          ents[k] = v;
        });
        if (Object.keys(ents).length === 0) { throw new Error('Command "' + label + '" needs at least one device (or use the clock).'); }
        cmd.entities = ents;
      }
    }
    out.push(cmd);
  });
  return out;
}
el('cmdsave').onclick = function(){
  var payload;
  try { payload = gatherCmds(); } catch (e) { el('cmdstatus').textContent = 'Not saved: ' + e.message; return; }
  el('cmdstatus').textContent = 'Saving commands...';
  el('cmdsave').disabled = true;
  fetch('/api/commands', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({version:1, commands:payload})}).then(function(r){return r.json();}).then(function(d){
    el('cmdsave').disabled = false;
    if (d.ok) { el('cmdstatus').textContent = 'Saved ' + d.count + ' commands. Backup: ' + (d.backup || 'none') + '. The voice service picks them up on its own.'; }
    else { el('cmdstatus').textContent = 'Save failed: ' + (d.error || 'unknown error'); }
  }).catch(function(e){ el('cmdsave').disabled = false; el('cmdstatus').textContent = 'Save request failed: ' + e; });
};
function runCmdTest(){
  var text = el('cmdtest').value.trim();
  if (text === '') { el('cmdtestresult').innerHTML = 'Type a phrase first.'; return; }
  el('cmdtestresult').innerHTML = 'Testing...';
  fetch('/api/matcher-test', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({text:text})}).then(function(r){return r.json();}).then(function(d){
    var html = '';
    if (d.error) { html = '<span class="no">Test failed:</span> ' + esc(d.error); }
    else if (!d.matched) {
      if (d.source === 'matcher-unavailable') { html = '<span class="no">No commands file yet.</span> Save commands first, then test again.'; }
      else { html = '<span class="no">No match.</span> This falls back to Decide, then Mercury 3.'; }
    }
    else if (d.source === 'keyword') {
      html = '<span class="ok">Built in: ' + esc(d.command_id) + '.</span> ' + esc(d.would || '');
    }
    else if (d.kind === 'do') {
      html = '<span class="ok">Matched: ' + esc(d.command_id) + ' (action).</span> It would ding';
      var av = d.target_available || {};
      var keys = Object.keys(av);
      if (keys.length > 0) {
        html += ' if the target is available: ' + keys.map(function(k){ return esc(k) + ' ' + (av[k] === true ? 'available' : (av[k] === false ? 'unavailable' : 'unknown')); }).join(', ');
      }
      html += '.';
      if (d.calls) { html += ' Calls: ' + esc(d.calls.map(function(cc){ return cc.domain + '.' + cc.service + ' ' + cc.entity_id; }).join('; ')) + '.'; }
    }
    else {
      html = '<span class="ok">Matched: ' + esc(d.command_id) + ' (question).</span> It would say: ' + (d.would ? '"' + esc(d.would) + '"' : 'nothing (unavailable, so it would fall back to Mercury 3).');
    }
    el('cmdtestresult').innerHTML = html;
  }).catch(function(e){ el('cmdtestresult').innerHTML = 'Test failed: ' + esc(String(e)); });
}
el('cmdtestbtn').onclick = runCmdTest;
el('cmdtest').addEventListener('keydown', function(e){ if (e.key === 'Enter') { runCmdTest(); } });
function loadCommands(){
  el('cmdstatus').textContent = 'Loading commands...';
  fetch('/api/commands').then(function(r){return r.json();}).then(function(d){
    if (d.error) { el('cmdstatus').textContent = 'Commands failed to load: ' + d.error; return; }
    cmds = (d.commands || []).map(function(cc){
      var out = {id:cc.id || '', label:cc.label || '', kind:cc.kind || 'do', device:cc.device || '', phrases:(cc.phrases || []).slice(), patterns:(cc.patterns || []).slice(), calls:[], entities:[], answer:cc.answer || '', source:cc.source};
      if (cc.kind === 'do') {
        out.calls = (cc.calls || []).map(function(call){
          var data = call.data || {};
          var ds = (data && Object.keys(data).length > 0) ? JSON.stringify(data) : '';
          return {service: call.domain + '.' + call.service, entity: call.entity_id || '', data: ds};
        });
      } else {
        var ents = cc.entities || {};
        Object.keys(ents).forEach(function(k){ out.entities.push({key:k, entity:ents[k]}); });
        if (cc.entity_id && !ents.primary) { out.entities.unshift({key:'primary', entity:cc.entity_id}); }
      }
      return out;
    });
    el('cmdstatus').textContent = '';
    renderCmds();
  }).catch(function(e){ el('cmdstatus').textContent = 'Commands failed to load: ' + e; });
}
loadCommands();
</script>
</body></html>
"""

RULES_PATH = "/home/user/mercury-engine/matcher_rules.json"
RULES_DO_DOMAINS = {"light", "switch", "vacuum", "timer", "script", "lock", "input_datetime", "input_boolean"}
RULES_STOP = {"stop", "shut up", "be quiet", "quiet", "silence", "stop that", "stop the noise", "turn it off", "stop the alarm", "stop the timer", "stop playing", "stop the sound", "shut the fuck up"}
RULES_TIMER_CANCEL = {"stop the timer", "cancel the timer", "stop my timer", "cancel my timer", "stop the office timer", "cancel the office timer"}
RULES_SLOTS = {"minutes", "message", "camera", "time"}


def _rules_norm(text):
    t = (text or "").lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t.rstrip(".!?").strip()


def validate_rules(payload):
    if not isinstance(payload, dict):
        raise ValueError("commands payload must be an object")
    commands = payload.get("commands")
    if not isinstance(commands, list):
        raise ValueError("commands must be a list")
    if len(commands) > 200:
        raise ValueError("too many commands (max 200)")
    seen_ids = set()
    seen_phrases = {}
    clean = []
    for pos, cmd in enumerate(commands):
        if not isinstance(cmd, dict):
            raise ValueError("command %d must be an object" % (pos + 1))
        cid = cmd.get("id")
        if not isinstance(cid, str) or re.fullmatch(r"[a-z0-9_]+", cid) is None:
            raise ValueError("command %d has a bad id" % (pos + 1))
        if cid in seen_ids:
            raise ValueError("duplicate command id: " + cid)
        seen_ids.add(cid)
        label = cmd.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("command " + cid + " needs a label")
        kind = cmd.get("kind")
        if kind not in ("do", "ask"):
            raise ValueError("command " + cid + " kind must be do or ask")
        phrases = cmd.get("phrases") or []
        patterns = cmd.get("patterns") or []
        if not isinstance(phrases, list) or not isinstance(patterns, list):
            raise ValueError("command " + cid + " phrases and patterns must be lists")
        if len(phrases) + len(patterns) == 0:
            raise ValueError("command " + cid + " needs at least one phrase or pattern")
        if len(phrases) > 50 or len(patterns) > 20:
            raise ValueError("command " + cid + " has too many phrases or patterns")
        for p in phrases:
            if not isinstance(p, str) or not p.strip():
                raise ValueError("command " + cid + " has an empty phrase")
            n = _rules_norm(p)
            if n in RULES_STOP or n in RULES_TIMER_CANCEL:
                raise ValueError('phrase "' + p + '" in ' + cid + " is built in and cannot be a command phrase")
            if n.startswith("ask muse") or n.startswith("tell muse"):
                raise ValueError('phrase "' + p + '" in ' + cid + " starts with Ask Muse and cannot be a command phrase")
            if n in seen_phrases:
                raise ValueError('phrase "' + p + '" is used by both ' + seen_phrases[n] + " and " + cid)
            seen_phrases[n] = cid
        for pat in patterns:
            if not isinstance(pat, str) or not pat.strip():
                raise ValueError("command " + cid + " has an empty pattern")
            i = 0
            while i < len(pat):
                ch = pat[i]
                if ch == "{":
                    j = pat.find("}", i)
                    if j < 0:
                        raise ValueError("pattern in " + cid + " has an unclosed slot")
                    slot = pat[i + 1:j]
                    if slot not in RULES_SLOTS:
                        raise ValueError("pattern in " + cid + " uses unknown slot " + slot)
                    i = j + 1
                elif ch == "}":
                    raise ValueError("pattern in " + cid + " has an unmatched }")
                else:
                    i += 1
        device = cmd.get("device")
        if not isinstance(device, str):
            raise ValueError("command " + cid + " device must be a string")
        out = {"id": cid, "label": label.strip(), "kind": kind, "device": device.strip(), "phrases": [p.strip() for p in phrases], "patterns": [p.strip() for p in patterns]}
        if kind == "do":
            calls = cmd.get("calls")
            if not isinstance(calls, list) or not calls:
                raise ValueError("command " + cid + " needs at least one call")
            clean_calls = []
            for call in calls:
                if not isinstance(call, dict):
                    raise ValueError("command " + cid + " has a bad call")
                if call.get("domain") not in RULES_DO_DOMAINS:
                    raise ValueError("command " + cid + " uses a disallowed domain")
                if not isinstance(call.get("service"), str) or not call.get("service"):
                    raise ValueError("command " + cid + " has a bad service")
                if not isinstance(call.get("entity_id"), str) or not call.get("entity_id"):
                    raise ValueError("command " + cid + " has a bad entity_id")
                data = call.get("data") or {}
                if not isinstance(data, dict):
                    raise ValueError("command " + cid + " data must be an object")
                clean_calls.append({"domain": call["domain"], "service": call["service"], "entity_id": call["entity_id"], "data": data})
            out["calls"] = clean_calls
        else:
            answer = cmd.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("command " + cid + " needs an answer")
            out["answer"] = answer.strip()
            if cmd.get("source") == "clock":
                out["source"] = "clock"
            else:
                ents = cmd.get("entities") or {}
                if cmd.get("entity_id"):
                    ents = dict(ents)
                    ents.setdefault("primary", cmd["entity_id"])
                if not isinstance(ents, dict) or not ents:
                    raise ValueError("command " + cid + " needs at least one device (or the clock)")
                for k, v in ents.items():
                    if not isinstance(k, str) or not k or not isinstance(v, str) or not v:
                        raise ValueError("command " + cid + " has a bad device entry")
                out["entities"] = ents
        clean.append(out)
    return 1, clean


def load_rules():
    try:
        with open(RULES_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {"version": 1, "commands": []}
    except Exception:
        return {"version": 1, "commands": [], "error": "rules file unreadable"}
    if not isinstance(raw, dict) or not isinstance(raw.get("commands"), list):
        return {"version": 1, "commands": [], "error": "rules file malformed"}
    return {"version": 1, "commands": raw["commands"]}


def save_rules(payload):
    version, commands = validate_rules(payload)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    backup = None
    if os.path.exists(RULES_PATH):
        backup = RULES_PATH + ".bak-" + stamp + "-commands"
        with open(RULES_PATH, "r", encoding="utf-8") as f:
            old = f.read()
        with open(backup, "w", encoding="utf-8") as f:
            f.write(old)
    tmp = RULES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": version, "commands": commands}, f, indent=2)
        f.write("\n")
    os.replace(tmp, RULES_PATH)
    return {"ok": True, "count": len(commands), "backup": backup}


def proxy_matcher_test(text):
    req = urllib.request.Request("http://127.0.0.1:8896/api/matcher/test", data=json.dumps({"text": text}).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))



def proxy_engine_json(path, payload, timeout):
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:8896" + path, data=data, method="POST" if payload is not None else "GET")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def proxy_engine_audio(path, payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:8896" + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", "audio/mpeg"), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "application/json"), e.read()

def proxy_engine_upload(path, raw, content_type, timeout):
    req = urllib.request.Request("http://127.0.0.1:8896" + path, data=raw, method="POST")
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self):
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"ok": True})
        elif self.path == "/":
            self.send_html()
        elif self.path == "/api/prompts":
            try:
                with LOCK:
                    data = get_prompts()
                self.send_json(200, data)
            except Exception as e:
                self.send_json(500, {"error": str(e)[:500]})
        elif self.path == "/api/loop-brief":
            try:
                text = load_text(LOOP_BRIEF)
                self.send_json(200, {"loop_brief": text})
            except Exception as e:
                self.send_json(500, {"error": str(e)[:500]})
        elif self.path == "/api/commands":
            try:
                with LOCK:
                    data = load_rules()
                self.send_json(200, data)
            except Exception as e:
                self.send_json(500, {"error": str(e)[:500]})
        elif self.path == "/api/engine-status" or self.path == "/api/poc-status":
            try:
                status, body = proxy_engine_json("/api/status", None, 10)
                try:
                    payload = json.loads(body.decode("utf-8"))
                except Exception:
                    payload = {"error": "bad engine response"}
                self.send_json(status, payload)
            except Exception as e:
                self.send_json(502, {"error": "engine status failed: " + str(e)[:200]})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/commands":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
            except Exception as e:
                self.send_json(400, {"error": "bad JSON: " + str(e)[:200]})
                return
            try:
                with LOCK:
                    result = save_rules(payload)
                self.send_json(200, result)
            except ValueError as e:
                self.send_json(400, {"error": str(e)[:500]})
            except Exception as e:
                self.send_json(500, {"error": str(e)[:500]})
            return
        if self.path == "/api/matcher-test":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
            except Exception as e:
                self.send_json(400, {"error": "bad JSON: " + str(e)[:200]})
                return
            text = (payload.get("text") or "").strip()
            if not text:
                self.send_json(400, {"error": "empty text"})
                return
            try:
                self.send_json(200, proxy_matcher_test(text))
            except Exception as e:
                self.send_json(502, {"error": "matcher test failed: " + str(e)[:200]})
            return
        if self.path in ("/api/engine-config", "/api/poc-config", "/api/engine-ask", "/api/poc-ask", "/api/engine-stop", "/api/poc-stop"):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length > 0 else b""
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception as e:
                self.send_json(400, {"error": "bad JSON: " + str(e)[:200]})
                return
            try:
                engine_path = {"/api/engine-config": "/api/config", "/api/poc-config": "/api/config", "/api/engine-ask": "/api/ask", "/api/poc-ask": "/api/ask", "/api/engine-stop": "/api/stop", "/api/poc-stop": "/api/stop"}[self.path]
                timeout = 90 if self.path in ("/api/engine-ask", "/api/poc-ask") else 30
                status, body = proxy_engine_json(engine_path, payload, timeout)
                try:
                    out = json.loads(body.decode("utf-8"))
                except Exception:
                    out = {"error": "bad engine response"}
                self.send_json(status, out)
            except Exception as e:
                self.send_json(502, {"error": "engine proxy failed: " + str(e)[:200]})
            return
        if self.path == "/api/engine-tts" or self.path == "/api/poc-tts":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
            except Exception as e:
                self.send_json(400, {"error": "bad JSON: " + str(e)[:200]})
                return
            try:
                status, ctype, body = proxy_engine_audio("/api/tts", payload, 60)
                self.send_bytes(status, ctype, body)
            except Exception as e:
                self.send_json(502, {"error": "engine tts failed: " + str(e)[:200]})
            return
        if self.path == "/api/engine-transcribe" or self.path == "/api/poc-transcribe":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length > 0 else b""
                ctype = self.headers.get("Content-Type", "")
            except Exception as e:
                self.send_json(400, {"error": "bad upload: " + str(e)[:200]})
                return
            if not raw:
                self.send_json(400, {"error": "empty audio"})
                return
            try:
                status, body = proxy_engine_upload("/api/transcribe", raw, ctype, 60)
                try:
                    out = json.loads(body.decode("utf-8"))
                except Exception:
                    out = {"error": "bad engine response"}
                self.send_json(status, out)
            except Exception as e:
                self.send_json(502, {"error": "engine transcribe failed: " + str(e)[:200]})
            return
        if self.path != "/api/save":
            self.send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self.send_json(400, {"error": "bad JSON: " + str(e)[:200]})
            return
        if "prompt" in payload and str(payload["prompt"]).strip() == "":
            self.send_json(400, {"error": "prompt must not be empty"})
            return
        lines = payload.get("lines", {}) or {}
        for key, val in lines.items():
            if str(val).strip() == "":
                self.send_json(400, {"error": "spoken line must not be empty: " + key})
                return
        try:
            result = apply_save(payload)
            self.send_json(200, result)
        except Exception as e:
            self.send_json(500, {"ok": False, "error": str(e)[:500]})


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("prompt-editor listening on 0.0.0.0:" + str(PORT), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()