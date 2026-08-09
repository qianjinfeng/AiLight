"""
agent_hook.py - AI agent hook client for the SN902W status light.

Installed as a Hook for Claude Code / Codex / Cursor / Copilot etc. When the
agent fires an event (session start, prompt submitted, tool running, error,
stop, session end...) this script is invoked, maps the event to a light
command via events.json, and reports it over TCP to the local daemon
(webserver.py), which then drives the SN902W LED.

Constraints (same as PromLight's hook):
  * Any failure silently exits 0 - never blocks the agent.
  * Never prints to stdout, EXCEPT the blocking/gating hooks (Cursor
    before* hooks and Antigravity) which must write a decision back.

Invocation (all equivalent):
    python agent_hook.py --agent claude            # reads JSON payload on stdin
    python agent_hook.py UserPromptSubmit --agent claude
"""

__version__ = "1.0.0"

import json
import os
import socket
import sys

_HOST = "127.0.0.1"
_DEFAULT_PORT = 47800
_CONNECT_TIMEOUT = 0.3

_SESSION_END_EVENTS = {"SessionEnd", "sessionEnd"}

_KNOWN_AGENTS = ("claude", "codex", "cursor", "copilot", "qoder", "codebuddy",
                 "antigravity", "opencode")

# Cursor blocking hooks must answer on stdout (never decide for the user).
_CURSOR_STDOUT = {
    "beforeSubmitPrompt":   {"continue": True},
    "beforeShellExecution": {"permission": "ask"},
    "beforeMCPExecution":   {"permission": "ask"},
}
# Antigravity is a gating hook: always allow on stdout, never block.
_ANTIGRAVITY_ALLOW = {"decision": "allow"}

# Built-in defaults - used only when events.json is missing or broken.
# Colors chosen for a single RGBW light (PromLight-style semantics).
_DEFAULT_MACROS = {
    "work":  "led yellow on --only --fade 300",
    "await": "led red blink --only --freq 1500 --fade 300",
    "idle":  "led green on --only --fade 800",
    "error": "led red on --only",
    "start": "led cyan blink --only --count 2 --freq 700 --fade 400 ; led green on --fade 800",
    "end":   "led green breath --only --freq 4000 --fade 2000",
}

_DEFAULT_EVENTS = {
    # Claude / Codex / Qoder / CodeBuddy (PascalCase)
    "SessionStart":        "start",
    "UserPromptSubmit":    "work",
    "PreToolUse":          "work",
    "PostToolUse":         "work",
    "PostToolUseFailure":  "error",
    "PermissionRequest":   "await",
    "PermissionDenied":    "await",
    "Elicitation":         "await",
    "SubagentStart":       "work",
    "SubagentStop":        "work",
    "PreCompact":          "work",
    "PostCompact":         "work",
    "Stop":                "idle",
    "SessionEnd":          "end",
    "StopFailure":         "error",
    # Cursor (camelCase)
    "sessionStart":         "start",
    "beforeSubmitPrompt":   "work",
    "afterFileEdit":        "work",
    "postToolUse":          "work",
    "beforeShellExecution": "await",
    "beforeMCPExecution":   "await",
    "stop":                 "idle",
    "sessionEnd":           "end",
    # Copilot CLI (camelCase)
    "userPromptSubmitted":  "work",
    "preToolUse":           "await",
    "agentStop":            "idle",
    "errorOccurred":        "error",
}
_MAX_MACRO_DEPTH = 8


def _config_path():
    v = os.environ.get("PROM_LIGHT_CONFIG", "").strip()
    if v:
        return v
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _events_path():
    v = os.environ.get("PROM_LIGHT_EVENTS", "").strip()
    if v:
        return v
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.json")


def _get_port():
    raw = os.environ.get("PROM_LIGHT_PORT", "")
    try:
        p = int(raw)
        if 1 <= p <= 65535:
            return p
    except (TypeError, ValueError):
        pass
    try:
        with open(_config_path(), "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
        p = int(cfg.get("hook_port") or cfg.get("port"))
        if 1 <= p <= 65535:
            return p
    except Exception:
        pass
    return _DEFAULT_PORT


def _load_config():
    path = _events_path()
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return {}, ""
    except Exception as e:
        return {}, "events.json 格式有误、已临时改用默认设置；请检查并修正：%s（%s）" % (path, e)
    if not isinstance(cfg, dict):
        return {}, "events.json 顶层应是一个对象 {…}、已临时改用默认设置；请检查：%s" % path
    bad = [n for n in ("events", "macros") if n in cfg and not isinstance(cfg.get(n), dict)]
    if bad:
        return cfg, "events.json 的 %s 段格式有误、被忽略并回退默认；请检查：%s" % ("、".join(bad), path)
    return cfg, ""


def _events_from(cfg):
    events = dict(_DEFAULT_EVENTS)
    raw = cfg.get("events") if isinstance(cfg, dict) else None
    if isinstance(raw, dict):
        events.update({str(k): str(v).strip() for k, v in raw.items()
                       if isinstance(v, str) and v.strip()})
    return events


def _macros_from(cfg):
    macros = dict(_DEFAULT_MACROS)
    raw = cfg.get("macros") if isinstance(cfg, dict) else None
    if isinstance(raw, dict):
        macros = {str(k).strip().lower(): str(v).strip() for k, v in raw.items()
                  if isinstance(v, str) and v.strip()}
    return macros


def _expand_macros(cmd, macros):
    def expand(text, depth, seen):
        out = []
        for clause in str(text).split(";"):
            c = clause.strip()
            if not c:
                continue
            key = c.lower()
            if key in macros and depth < _MAX_MACRO_DEPTH and key not in seen:
                out.append(expand(macros[key], depth + 1, seen | {key}))
            else:
                out.append(c)
        return " ; ".join(out)
    try:
        return expand(cmd, 0, frozenset())
    except Exception:
        return cmd


def _clip(value, n=120):
    s = " ".join(str(value).split())
    return s if len(s) <= n else s[:n] + "…"


def _read_payload():
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.buffer.read()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        obj = json.loads(raw.decode("utf-8-sig", "replace"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _parse_args(argv):
    agent, positional = "", ""
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--agent":
            agent = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        if a.startswith("--agent="):
            agent = a[len("--agent="):]
            i += 1
            continue
        if not positional and not a.startswith("-"):
            positional = a
        i += 1
    return agent.strip(), positional


def _emit_stdout(obj):
    try:
        sys.stdout.write(json.dumps(obj))
        sys.stdout.flush()
    except Exception:
        pass


def _build_message(event, payload):
    detail = {}
    for key in ("tool_name", "prompt", "command", "file_path", "query", "url"):
        if payload.get(key) not in (None, ""):
            detail[key] = _clip(payload[key])
    tail = "  ".join("%s=%s" % kv for kv in detail.items())
    return ("%s  %s" % (event, tail)).rstrip()


def _norm_session(payload):
    return (payload.get("session_id") or payload.get("conversation_id")
            or payload.get("sessionId"))


def _norm_cwd(payload):
    cwd = payload.get("cwd")
    if cwd:
        return cwd
    roots = payload.get("workspace_roots") or payload.get("workspacePaths")
    if isinstance(roots, list) and roots:
        first = roots[0]
        if isinstance(first, dict):
            first = first.get("path") or first.get("root") or first.get("uri")
        if isinstance(first, str) and first:
            return first
    return None


def main(argv):
    payload = _read_payload()
    agent, positional = _parse_args(argv)
    agent = agent.lower()
    if agent not in _KNOWN_AGENTS:
        agent = ""
    event = (payload.get("hook_event_name") or positional
             or os.environ.get("PROM_LIGHT_HOOK_EVENT", ""))
    if not event:
        return 0

    if agent == "cursor" and event in _CURSOR_STDOUT:
        _emit_stdout(_CURSOR_STDOUT[event])
    elif agent == "antigravity":
        _emit_stdout(_ANTIGRAVITY_ALLOW)

    cfg, notice = _load_config()
    cmd = _events_from(cfg).get(event)
    release = event in _SESSION_END_EVENTS
    if cmd is None and not release:
        return 0
    if cmd:
        cmd = _expand_macros(cmd, _macros_from(cfg))

    msg = json.dumps({
        "cmd": cmd or "",
        "message": _build_message(event, payload),
        "session": _norm_session(payload),
        "cwd": _norm_cwd(payload),
        "agent": agent,
        "release_session": release,
        "notice": notice,
        "event": event,
    }) + "\n"
    try:
        with socket.create_connection((_HOST, _get_port()), timeout=_CONNECT_TIMEOUT) as s:
            s.sendall(msg.encode("utf-8"))
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        sys.exit(0)
