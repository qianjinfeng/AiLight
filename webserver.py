#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webserver.py - SN902W status-light daemon (PromLight-style).

Turns a Sleepace SN902W ("Nox 902W") into a Bluetooth status light that mirrors
your AI agent's state (working / waiting / done / error).

Components
----------
  * BLE connection to the SN902W (bleak) with auto-reconnect
  * TCP "hook server" on 127.0.0.1:<hook_port> (default 47800) that agent_hook.py
    uses to report agent events
  * HTTP web console on http://127.0.0.1:<web_port> (default 7800)
  * light-effect engine (solid / blink / breath) driving the device's RGBW LED
  * `setup` command that writes Hook configs for Claude Code / Codex / Cursor /
    Copilot

Usage
-----
    python webserver.py                  # run daemon (auto-connect to device)
    python webserver.py --dry-run        # run without the physical device
    python webserver.py --setup all      # write agent hooks, then exit
    python webserver.py --scan           # list nearby SN902 devices, then exit

Web console
-----------
    http://127.0.0.1:7800
    led green on --only --fade 800
    led yellow blink --count 5 --freq 700
    devices
    setup claude
    help
"""

import argparse
import asyncio
import json
import logging
import os
import shlex
import shutil
import socket
import sqlite3
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import sn902

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    "ble_address": "",          # pin one device by MAC to skip scanning entirely
    "ble_addresses": [],        # extra pinned MACs to try (in order) before scanning
    "ble_name_filter": "",      # optional substring filter for the advertised name
    "name_fragments": ["SN22", "SN-21", "SN", "Nox 902", "902B", "902W"],
    "scan_timeout": 8,
    "web_port": 7800,
    "hook_port": 47800,
    "brightness": 50,           # 1-100 percent
    "default_color": "cyan",
    "reconnect_delay": 3,
    "auto_connect": True,
    # opencode activity watcher (reads the local opencode.db event table)
    "opencode_watch": True,
    "opencode_db": "",          # auto-detected: ~/.local/share/opencode/opencode.db
    "opencode_idle_ms": 4000,   # trail busy for this long after the last event
    "opencode_poll_s": 1.0,
}

# named colors for the single RGBW light
COLORS = {
    "green":   (0, 220, 60),
    "red":     (255, 40, 40),
    "yellow":  (255, 200, 0),
    "orange":  (255, 130, 0),
    "cyan":    (0, 230, 220),
    "blue":    (30, 90, 255),
    "magenta": (255, 40, 255),
    "purple":  (170, 60, 255),
    "pink":    (255, 90, 170),
}

# hook config targets for `setup`
AGENTS = ("claude", "codex", "cursor", "copilot", "opencode", "vscode",
          "qoder", "codebuddy", "antigravity")

CLAUDE_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
                      "PostToolUseFailure", "PermissionRequest", "PermissionDenied",
                      "Elicitation", "SubagentStart", "SubagentStop", "Stop", "SessionEnd",
                      "StopFailure")
CODEX_HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest",
                     "Stop", "SessionEnd")
CURSOR_HOOK_EVENTS = ("sessionStart", "beforeSubmitPrompt", "afterFileEdit", "postToolUse",
                      "beforeShellExecution", "beforeMCPExecution", "stop", "sessionEnd")
# Copilot CLI native hook events (docs.github.com/.../agents/hooks) - camelCase.
# NOTE: Copilot CLI has NO "waiting for user" (await) signal - its approval
# prompts are interactive UI, and preToolUse (the only event that fires near
# them) is a blocking hook that also fires on every auto-approved tool call,
# so it is deliberately not mapped.
COPILOT_HOOK_EVENTS = ("sessionStart", "userPromptSubmitted", "postToolUse",
                       "agentStop", "sessionEnd", "errorOccurred")

log = logging.getLogger("webserver")

# ------------------------------------------------------------------------------
# config / events
# ------------------------------------------------------------------------------
def config_path(args=None):
    if args is not None and args.config:
        return args.config
    v = os.environ.get("SN902_CONFIG", "").strip()
    if v:
        return v
    return os.path.join(SCRIPT_DIR, "config.json")


def load_config(args=None):
    cfg = dict(DEFAULT_CONFIG)
    path = config_path(args)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                user = json.load(f)
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if v is not None})
        except Exception as e:
            log.warning("config.json invalid (%s), using defaults", e)
    if args is not None:
        if args.web_port:
            cfg["web_port"] = args.web_port
        if args.hook_port:
            cfg["hook_port"] = args.hook_port
        if args.address:
            cfg["ble_address"] = args.address
    return cfg


def save_config(cfg):
    path = os.path.join(SCRIPT_DIR, "config.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning("could not save config.json: %s", e)


def load_events():
    """Load events.json -> (events, macros, notice)."""
    defaults_events = {
        "SessionStart": "start", "UserPromptSubmit": "work", "PreToolUse": "work",
        "PostToolUse": "work", "PostToolUseFailure": "error", "PermissionRequest": "await",
        "PermissionDenied": "await", "Elicitation": "await", "SubagentStart": "work",
        "SubagentStop": "work", "PreCompact": "work", "PostCompact": "work",
        "Stop": "idle", "SessionEnd": "end", "StopFailure": "error",
        "sessionStart": "start", "beforeSubmitPrompt": "work", "afterFileEdit": "work",
        "postToolUse": "work", "beforeShellExecution": "await", "beforeMCPExecution": "await",
        "stop": "idle", "sessionEnd": "end",
        "userPromptSubmitted": "work", "agentStop": "idle", "errorOccurred": "error",
        # VS Code Copilot (reported by the sn902-copilot-status extension)
        "copilot.chat.start": "work", "copilot.chat.request": "work",
        "copilot.chat.progress": "work", "copilot.chat.complete": "idle",
        "copilot.chat.error": "error",
        "copilot.suggest.work": "work", "copilot.suggest.idle": "idle",
        "copilot.suggest.error": "error",
        # opencode (reported by the built-in SQLite watcher; the opencode plugin
        # API has no session-event hook on desktop 1.18.x)
        "opencode.busy": "work", "opencode.idle": "idle",
        "opencode.error": "error", "opencode.await": "await",
    }
    defaults_macros = {
        "work":  "led yellow on --only --fade 300",
        "await": "led yellow blink --only --freq 1500 --fade 300",
        "idle":  "led green on --only --fade 800",
        "error": "led red blink --only --count 5 --freq 700",
        "start": "led cyan blink --only --count 2 --freq 700 --fade 400 ; led green on --fade 800",
        "end":   "led green breath --only --freq 4000 --fade 2000",
    }
    path = os.path.join(SCRIPT_DIR, "events.json")
    cfg = {}
    notice = ""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:
        notice = "events.json 格式有误、已用默认映射：%s" % e
    events = dict(defaults_events)
    macros = dict(defaults_macros)
    if isinstance(cfg, dict):
        if isinstance(cfg.get("events"), dict):
            events.update({str(k): str(v).strip() for k, v in cfg["events"].items()
                           if isinstance(v, str) and v.strip()})
        if isinstance(cfg.get("macros"), dict):
            macros.update({str(k).strip().lower(): str(v).strip() for k, v in cfg["macros"].items()
                           if isinstance(v, str) and v.strip()})
    else:
        notice = notice or "events.json 顶层应为对象，已用默认映射"
    return events, macros, notice


def expand_macros(cmd, macros, depth=0, seen=None):
    if depth > 8:
        return cmd
    seen = seen or set()
    out = []
    for clause in cmd.split(";"):
        c = clause.strip()
        if not c:
            continue
        key = c.lower()
        if key in macros and key not in seen:
            out.append(expand_macros(macros[key], macros, depth + 1, seen | {key}))
        else:
            out.append(c)
    return " ; ".join(out)


# ------------------------------------------------------------------------------
# App state
# ------------------------------------------------------------------------------
class App:
    def __init__(self, config, loop, dry_run=False):
        self.config = config
        self.loop = loop
        self.dry_run = dry_run
        self.events, self.macros, self.events_notice = load_events()
        self.event_log = []
        self.event_id = 0
        self.device = sn902.SN902Device(dry_run=dry_run)
        self.engine = LightEngine(self, loop)
        self.connected = dry_run
        self.last_effect = None
        self.started = time.time()
        self.scan_task = None
        self.brightness_percent = int(config.get("brightness", 50))
        self.adapter_error = ""
        self.adapter_notified = False
        self._lock = threading.Lock()

    # -- events ---------------------------------------------------------------
    def log(self, level, message, kind="system"):
        with self._lock:
            self.event_id += 1
            ev = {"id": self.event_id, "t": time.time(), "level": level,
                  "kind": kind, "message": str(message)[:2000]}
            self.event_log.append(ev)
            if len(self.event_log) > 1000:
                del self.event_log[:300]
        return ev

    def events_since(self, since):
        with self._lock:
            return [e for e in self.event_log if e["id"] > since]

    def set_connected(self, state, detail=""):
        self.connected = state
        self.log("info", ("BLE connected" if state else "BLE disconnected") +
                 (" (%s)" % detail if detail else ""), kind="connection")

    def brightness_byte(self):
        p = max(0, min(100, self.brightness_percent))
        return int(round(p * 255 / 100.0))

    def state_dict(self):
        d = self.device
        return {
            "ok": True,
            "connected": bool(self.connected),
            "dry_run": bool(self.dry_run),
            "adapter": {
                "ok": not self.adapter_error,
                "error": self.adapter_error,
            },
            "device": {
                "name": d.device_name,
                "address": d.device_address,
                "connected_at": d.connected_at,
                "last_error": d.last_error,
            },
            "effect": self.engine.current,
            "brightness": self.brightness_percent,
            "default_color": self.config.get("default_color", "cyan"),
            "web_port": self.config.get("web_port", 7800),
            "hook_port": self.config.get("hook_port", 47800),
            "uptime": round(time.time() - self.started, 1),
            "events_total": self.event_id,
            "events_notice": self.events_notice,
        }


# ------------------------------------------------------------------------------
# Light engine
# ------------------------------------------------------------------------------
class LightEngine:
    """Latest-wins effect engine.

    A single worker task pops effects from a queue; only the most recent
    requested effect survives. When a new effect arrives it cancels the
    currently-playing effect. Safe to call `schedule` from any thread.
    """

    def __init__(self, app, loop):
        self.app = app
        self.loop = loop
        self._queue = asyncio.Queue(maxsize=1)
        self._worker = None
        self._play_task = None
        self.current = None

    def schedule(self, effect):
        """Queue an effect; drop any effect not yet started (latest wins)."""
        effect = dict(effect)

        async def _start():
            if self._worker is None or self._worker.done():
                self._worker = asyncio.ensure_future(self.worker())
            # latest-wins: clear anything still waiting in the queue
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            await self._queue.put(effect)

        return asyncio.run_coroutine_threadsafe(_start(), self.loop)

    def stop(self):
        def _stop():
            if self._play_task is not None:
                self._play_task.cancel()
            if self._worker is not None:
                self._worker.cancel()
        asyncio.run_coroutine_threadsafe(_stop(), self.loop)

    async def worker(self):
        while True:
            eff = await self._queue.get()
            if self._play_task is not None:
                self._play_task.cancel()
                try:
                    await self._play_task
                except (Exception, asyncio.CancelledError):
                    pass
            self._play_task = None
            self.current = eff
            self.app.last_effect = eff
            self.app.log("info", "light: %s" % describe(eff), kind="light")
            self._play_task = asyncio.ensure_future(self._play(eff))

    async def _play(self, eff):
        try:
            await self._play_inner(eff)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.app.log("error", "light effect error: %s" % e, kind="light")

    async def _play_inner(self, eff):
        device = self.app.device
        action = eff.get("action", "on")
        if action == "off":
            await self._fade_down(device, eff)
            await device.light_off()
            self.current = None
            return
        if action == "on":
            if eff.get("fade_ms", 0) > 0:
                await self._ramp(device, eff, 0, eff["brightness"], eff["fade_ms"])
            else:
                await self._write(device, eff, eff["brightness"])
            return
        if action == "blink":
            half = max(50, int(eff.get("freq_ms", 1000) / 2))
            count = int(eff.get("count", 0))
            while count == 0 or count > 0:
                await self._write(device, eff, eff["brightness"])
                await asyncio.sleep(half / 1000.0)
                await device.light_off()
                await asyncio.sleep(half / 1000.0)
                if count > 0:
                    count -= 1
            return
        if action == "breath":
            freq = max(600, int(eff.get("freq_ms", 4000)))
            while True:
                await self._ramp(device, eff, 0, eff["brightness"], freq / 2)
                await self._ramp(device, eff, eff["brightness"], 0, freq / 2)
            return

    async def _write(self, device, eff, brightness):
        if eff.get("white"):
            await device.set_white(eff.get("w", 255), int(brightness))
        else:
            r, g, b = eff["rgb"]
            await device.set_color(r, g, b, eff.get("w", 0), int(brightness))

    async def _ramp(self, device, eff, from_b, to_b, duration_ms):
        duration_ms = max(0, int(duration_ms))
        steps = max(1, int(duration_ms / 50))
        if steps == 1:
            await self._write(device, eff, to_b)
            return
        for i in range(1, steps + 1):
            b = from_b + (to_b - from_b) * i / steps
            await self._write(device, eff, int(round(b)))
            await asyncio.sleep(duration_ms / steps / 1000.0)

    async def _fade_down(self, device, eff, duration_ms=300):
        try:
            await self._ramp(device, eff, eff.get("brightness", 0), 0, duration_ms)
        except Exception:
            pass


def describe(effect):
    action = effect.get("action")
    if effect.get("white"):
        color = "white"
    else:
        r, g, b = effect.get("rgb", (0, 0, 0))
        color = "#%02x%02x%02x" % (r, g, b)
    s = "%s %s @%d%%" % (action, color, effect.get("brightness", 0) * 100 // 255)
    if action in ("blink", "breath"):
        s += " freq=%dms" % effect.get("freq_ms", 1000)
    if effect.get("count"):
        s += " x%d" % effect["count"]
    return s


# ------------------------------------------------------------------------------
# BLE worker (connect / reconnect)
# ------------------------------------------------------------------------------
async def connect_once(app):
    cfg = app.config
    dev = app.device

    # 1) pinned addresses first (deterministic)
    pinned = []
    if cfg.get("ble_address"):
        pinned.append(str(cfg["ble_address"]).strip())
    pinned += [str(a).strip() for a in (cfg.get("ble_addresses") or [])
               if str(a).strip() and str(a).strip().lower() not in [p.lower() for p in pinned]]
    for addr in pinned:
        if await dev.connect(addr):
            log.info("connected to pinned device %s", addr)
            return True
        log.info("pinned %s failed: %s", addr, dev.last_error)
        dev.last_error = None

    # 2) scan, then try each candidate until one connects
    log.info("scanning for Nox 902W device ...")
    found, _all, adapter_ok, adapter_error = await sn902.SN902Device.scan(
        timeout=float(cfg.get("scan_timeout", 8)),
        name_fragments=cfg.get("name_fragments", sn902.NAME_FRAGMENTS))
    if not adapter_ok:
        app.adapter_error = adapter_error
        if not app.adapter_notified:
            app.adapter_notified = True
            app.log("warn",
                    "no Bluetooth adapter found (%s) - plug in / enable Bluetooth, "
                    "then reconnect" % adapter_error, kind="connection")
        return False
    app.adapter_error = ""
    app.adapter_notified = False
    if not found:
        app.log("warn", "scan finished, no Nox 902W device found "
                "(is it powered on, awake and within ~5 m?)", kind="connection")
        return False
    filt = str(cfg.get("ble_name_filter") or "").strip().lower()
    candidates = found
    if filt:
        candidates = [f for f in found if filt in (f[0] or "").lower()]
    if not candidates:
        app.log("warn", "no device matched name filter '%s' (found: %s)" %
                (filt, ", ".join(f[0] or "?" for f in found)), kind="connection")
        return False
    for name, addr, rssi in candidates:
        if await dev.connect((name, addr)):
            log.info("connected to %s (%s) rssi=%s", name, addr, rssi)
            return True
        log.warning("candidate %s (%s) connect failed: %s", name, addr, dev.last_error)
        dev.last_error = None
    return False


async def ble_worker(app):
    delay = float(app.config.get("reconnect_delay", 3))
    while True:
        dev = app.device
        if not dev.connected:
            if app.config.get("auto_connect", True):
                ok = await connect_once(app)
                if ok:
                    app.set_connected(True)
                    try:
                        await dev.login()
                    except Exception:
                        pass
                    try:
                        await dev.sync_time()
                    except Exception:
                        pass
                    if app.last_effect is not None:
                        app.engine.schedule(app.last_effect)
                else:
                    app.set_connected(False)
            await asyncio.sleep(delay)
            continue
        if dev.dry_run:
            await asyncio.sleep(1.0)
            continue
        try:
            is_conn = bool(dev.client and dev.client.is_connected)
        except Exception:
            is_conn = False
        if not is_conn:
            app.log("info", "device link lost, reconnecting ...", kind="connection")
            await dev.disconnect()
            app.set_connected(False)
        await asyncio.sleep(2.0)


# ------------------------------------------------------------------------------
# Hook server (TCP 47800) - receives agent_hook.py messages
# ------------------------------------------------------------------------------
async def handle_hook_conn(app, reader, writer):
    try:
        data = await asyncio.wait_for(reader.readline(), timeout=3)
    except Exception:
        writer.close()
        return
    if not data:
        writer.close()
        return
    try:
        msg = json.loads(data.decode("utf-8", "replace").strip() or "{}")
    except Exception as e:
        _hook_reply(writer, {"ok": False, "detail": "bad json: %s" % e})
        return
    if not isinstance(msg, dict):
        _hook_reply(writer, {"ok": False, "detail": "expected a json object"})
        return

    cmd = str(msg.get("cmd") or "").strip()
    event = str(msg.get("event") or "").strip()
    notice = str(msg.get("notice") or "").strip()
    if not cmd:
        # fallback: map the event via events.json ourselves
        if event:
            macro = app.events.get(event)
            if macro:
                cmd = expand_macros(macro, app.macros)
    if not cmd and msg.get("release_session"):
        cmd = "led green on --only"

    message = str(msg.get("message") or "")
    if not message:
        message = "agent event: %s" % event if event else cmd
    if message:
        app.log("info", message, kind="hook")
    if notice:
        app.log("warn", notice, kind="hook")

    result = execute_cmd(app, cmd, source="hook")
    result["event"] = event
    _hook_reply(writer, result)


def _hook_reply(writer, obj):
    try:
        writer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def hook_server(app):
    server = await asyncio.start_server(
        lambda r, w: handle_hook_conn(app, r, w), "127.0.0.1", int(app.config["hook_port"]))
    app.log("info", "hook server listening on 127.0.0.1:%s" % app.config["hook_port"])
    async with server:
        await server.serve_forever()


# ------------------------------------------------------------------------------
# opencode activity watcher
#
# OpenCode Desktop (>=1.18) uses an Effect-based plugin API with no session-event
# hook, so a plugin cannot observe "busy/idle". Instead we tail the event table
# of the local SQLite store (~/.local/share/opencode/opencode.db): while the
# agent is working it keeps appending event rows (message.part.updated.1 etc.),
# and when it goes idle the writes stop. We treat "recent events" as busy and a
# quiet period (opencode_idle_ms) as idle, and report transitions to the light.
# ------------------------------------------------------------------------------
def _opencode_db_path(cfg):
    p = str(cfg.get("opencode_db") or "").strip()
    if p:
        return os.path.expanduser(p)
    return os.path.join(os.path.expanduser("~"), ".local", "share", "opencode", "opencode.db")


def _opencode_poll(db, last_seq):
    """Return (max_seq, has_activity, has_error) since last_seq. Never raises."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
        try:
            cur = con.cursor()
            cur.execute("SELECT MAX(seq) FROM event")
            row = cur.fetchone()
            if row is None or row[0] is None:
                return None, False, False
            max_seq = int(row[0])
            if max_seq <= last_seq:
                return max_seq, False, False
            cur.execute("SELECT type, data FROM event WHERE seq > ? ORDER BY seq LIMIT 800",
                        (last_seq,))
            rows = cur.fetchall()
        finally:
            con.close()
        has_error = False
        for typ, data in rows:
            if "error" in typ.lower() or "fail" in typ.lower():
                has_error = True
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if isinstance(obj, dict):
                part = obj.get("part")
                if isinstance(part, dict) and part.get("type") == "tool":
                    st = part.get("state") or {}
                    if st.get("status") in ("error", "failed"):
                        has_error = True
        return max_seq, True, has_error
    except Exception as e:
        log.warning("opencode watcher db error: %s", e)
        return None, False, False


def _opencode_apply(app, macro):
    try:
        cmd = expand_macros(macro, app.macros)
        if cmd:
            execute_cmd(app, cmd, source="opencode")
    except Exception as e:
        log.warning("opencode apply failed: %s", e)


def opencode_watcher_thread(app):
    db = _opencode_db_path(app.config)
    idle_ms = int(app.config.get("opencode_idle_ms", 4000))
    poll_s = max(0.2, float(app.config.get("opencode_poll_s", 1.0)))
    last_seq = -1
    last_activity = 0.0
    state = None
    missing_logged = False
    while True:
        time.sleep(poll_s)
        if not os.path.exists(db):
            if not missing_logged:
                missing_logged = True
                app.log("warn", "opencode watcher: db not found yet (%s)" % db,
                        kind="opencode")
            continue
        missing_logged = False
        max_seq, activity, has_error = _opencode_poll(db, last_seq)
        if max_seq is not None:
            last_seq = max_seq
        if activity:
            last_activity = time.time()
            if state != "busy":
                state = "busy"
                app.log("info", "opencode: busy", kind="opencode")
                _opencode_apply(app, app.events.get("opencode.busy") or "work")
            elif has_error and state != "error":
                state = "error"
                app.log("info", "opencode: error", kind="opencode")
                _opencode_apply(app, app.events.get("opencode.error") or "error")
            continue
        if state == "busy" and has_error and state != "error":
            state = "error"
            app.log("info", "opencode: error", kind="opencode")
            _opencode_apply(app, app.events.get("opencode.error") or "error")
        if state in ("busy", "error") and (time.time() - last_activity) * 1000 > idle_ms:
            state = "idle"
            app.log("info", "opencode: idle", kind="opencode")
            _opencode_apply(app, app.events.get("opencode.idle") or "idle")


# ------------------------------------------------------------------------------
# command parser (shared by web console + hook server)
# ------------------------------------------------------------------------------
def parse_opts(parts):
    opts = {"only": False, "count": 0, "freq": None, "fade": None}
    i = 0
    while i < len(parts):
        p = parts[i]
        if p == "--only":
            opts["only"] = True
        elif p in ("--count", "--freq", "--fade"):
            val = parts[i + 1] if i + 1 < len(parts) else ""
            opts[p[2:]] = parse_ms(val)
            i += 1
        i += 1
    return opts


def parse_ms(val):
    """Parse '1500' / '1500ms' / '1.5s' -> int milliseconds."""
    try:
        v = str(val).strip().lower()
        if v.endswith("ms"):
            return int(round(float(v[:-2])))
        if v.endswith("s"):
            return int(round(float(v[:-1]) * 1000))
        return int(round(float(v)))
    except Exception:
        return 0


def resolve_color(app, color):
    if color == "white":
        return {"white": True, "rgb": (255, 255, 255), "w": app.brightness_byte()}
    if color == "all":
        color = str(app.config.get("default_color", "cyan")).lower()
    if color.startswith("#"):
        h = color[1:]
        if len(h) == 6:
            try:
                r = int(h[0:2], 16); g = int(h[2:4], 16); b = int(h[4:6], 16)
                return {"white": False, "rgb": (r, g, b), "w": 0}
            except ValueError:
                return None
        if len(h) == 3:
            try:
                r = int(h[0] * 2, 16); g = int(h[1] * 2, 16); b = int(h[2] * 2, 16)
                return {"white": False, "rgb": (r, g, b), "w": 0}
            except ValueError:
                return None
        return None
    if color in COLORS:
        r, g, b = COLORS[color]
        return {"white": False, "rgb": (r, g, b), "w": 0}
    return None


def cmd_led(app, parts):
    opts = parse_opts(parts)
    positional = [p for p in parts[1:] if not p.startswith("--")]
    color = action = None
    for p in positional:
        low = p.lower()
        if action is None and low in ("on", "off", "blink", "breath", "fade"):
            action = low
        elif color is None:
            color = p
    if color is None:
        return {"ok": False, "detail": "usage: led <color|all|#hex> <on|off|blink|breath> [--count N] [--freq N|Nms|Ns] [--fade N] [--only]"}
    if color.lower() == "off":
        action = "off"
    if action is None:
        action = "on" if color.lower() != "all" else "on"

    resolved = resolve_color(app, color.lower())
    if resolved is None:
        return {"ok": False, "detail": "unknown color '%s' (known: %s, #rrggbb)" %
                (color, ", ".join(list(COLORS) + ["all", "white", "off"]))}

    if action == "fade":
        action = "breath" if opts.get("fade") else "on"

    effect = dict(resolved)
    effect["action"] = action
    effect["brightness"] = app.brightness_byte()
    effect["count"] = int(opts.get("count") or 0)
    if action == "blink":
        effect["freq_ms"] = opts.get("freq") or 1000
    elif action == "breath":
        effect["freq_ms"] = opts.get("freq") or 4000
    else:
        effect["freq_ms"] = opts.get("freq") or 0
    effect["fade_ms"] = opts.get("fade") or (300 if action in ("on", "off") else 0)

    app.engine.schedule(effect)
    return {"ok": True, "detail": "queued: %s" % describe(effect)}


def cmd_write(app, parts):
    # write led.brightness 60
    if len(parts) >= 3 and parts[1].lower() == "led.brightness":
        try:
            val = int(float(parts[2]))
        except (ValueError, IndexError):
            return {"ok": False, "detail": "usage: write led.brightness <1-100>"}
        app.brightness_percent = max(1, min(100, val))
        app.config["brightness"] = app.brightness_percent
        save_config(app.config)
        app.log("info", "brightness set to %d%%" % app.brightness_percent, kind="light")
        if app.engine.current:
            app.engine.schedule(app.engine.current)
        return {"ok": True, "detail": "brightness = %d%%" % app.brightness_percent}
    return {"ok": False, "detail": "only supported: write led.brightness <1-100>"}


def cmd_read(app, parts):
    if len(parts) >= 2 and parts[1].lower() == "led.brightness":
        return {"ok": True, "detail": "led.brightness = %d" % app.brightness_percent}
    return {"ok": False, "detail": "only supported: read led.brightness"}


def cmd_devices(app):
    d = app.device
    if app.dry_run:
        return {"ok": True, "detail": "dry-run mode (no real BLE). device: SN902W (simulated)",
                "devices": [{"name": "SN902W", "address": "DRY-RUN", "connected": True}]}
    if d.connected:
        line = "%s  %s  %s" % ("CONNECTED", d.device_name or "?", d.device_address or "?")
        return {"ok": True, "detail": line,
                "devices": [{"name": d.device_name, "address": d.device_address, "connected": True}]}
    line = "NOT CONNECTED"
    if d.last_error:
        line += " (%s)" % d.last_error
    return {"ok": True, "detail": line, "devices": []}


HELP_TEXT = """commands (run in the web console at http://127.0.0.1:%s):
  devices                     show the SN902W connection state
  scan                        rescan for the device
  led <color> <action> [opts] control the light
      color: %s, all, white, off, #rrggbb
      action: on | off | blink | breath
      opts: --only  --count N  --freq N|Nms|Ns  --fade N|Nms|Ns
  write led.brightness <1-100>  set default brightness
  read led.brightness           show brightness
  setup <agent|all>             install agent Hook/plugin/extension configs
                               (claude/codex/cursor/copilot/opencode/vscode)
  reload                        reload events.json
  help                          this help
"""


def cmd_setup(app, parts):
    agent = parts[1].lower() if len(parts) > 1 else "all"
    if agent == "all":
        targets = detect_agents()
    elif agent in AGENTS:
        targets = [agent]
    else:
        return {"ok": False, "detail": "unknown agent '%s' (expected: %s or all)" %
                (agent, "/".join(AGENTS))}
    if not targets:
        return {"ok": True, "detail": "no supported agents detected on this machine (claude/codex/cursor/copilot). "
                                       "Install one, or run 'setup <agent>' explicitly."}
    lines = []
    for a in targets:
        ok, detail = write_agent_hooks(app, a)
        lines.append("%-10s %s" % (a, "OK" if ok else "SKIP/ERR") + (" - " + detail if detail else ""))
    app.log("info", "setup: " + "; ".join(lines), kind="system")
    return {"ok": True, "detail": "\n".join(lines)}


def execute_cmd(app, cmd, source="console"):
    cmd = (cmd or "").strip()
    if not cmd:
        return {"ok": False, "detail": "empty command"}
    # support ';'-chained commands (e.g. "start"/"end" macros)
    clauses = [c.strip() for c in cmd.split(";") if c.strip()]
    if len(clauses) > 1:
        results = [execute_cmd(app, c, source) for c in clauses]
        return {"ok": all(r.get("ok") for r in results),
                "detail": " ; ".join(r.get("detail", "") for r in results)}
    cmd = clauses[0]
    # route: @alias ...
    if cmd.startswith("@"):
        idx = cmd.find(" ")
        cmd = cmd[idx + 1:].strip() if idx != -1 else "devices"
    try:
        parts = shlex.split(cmd)
    except ValueError as e:
        return {"ok": False, "detail": "parse error: %s" % e}
    if not parts:
        return {"ok": False, "detail": "empty command"}

    verb = parts[0].lower()
    if verb in ("help", "?"):
        return {"ok": True, "detail": HELP_TEXT % (app.config.get("web_port"), ", ".join(COLORS))}
    if verb in ("devices", "device", "status", "state"):
        return cmd_devices(app)
    if verb == "scan":
        if not app.dry_run:
            show_all = (len(parts) > 1 and parts[1].lower() in ("all", "--all"))
            async def _scan():
                return await sn902.SN902Device.scan(
                    timeout=float(app.config.get("scan_timeout", 8)),
                    name_fragments=app.config.get("name_fragments", sn902.NAME_FRAGMENTS))
            try:
                found, all_dev, adapter_ok, adapter_error = \
                    asyncio.run_coroutine_threadsafe(_scan(), app.loop).result(timeout=20)
            except Exception as e:
                return {"ok": False, "detail": "scan failed: %s" % e}
            if not adapter_ok:
                app.adapter_error = adapter_error
                return {"ok": False,
                        "detail": "no Bluetooth adapter found (%s) - plug in / enable Bluetooth" % adapter_error}
            app.adapter_error = ""
            if show_all or not found:
                def _is_candidate(n, svcs):
                    if n and any(f in n for f in app.config.get("name_fragments", sn902.NAME_FRAGMENTS)):
                        return True
                    return any(u.lower().startswith(s) for s in sn902.MATCH_SERVICES
                               for u in (svcs or []))
                lines = ["%d device(s) on the air:" % len(all_dev)]
                for n, a, r, _md, sv in all_dev:
                    mark = "*" if _is_candidate(n, sv) else " "
                    lines.append("%s %-26s %s  rssi=%s" % (mark, n or "(no name)", a, r))
                if found:
                    lines.append("")
                    lines.append("matches: " + ", ".join(n or a for n, a, _r in found))
                detail = "\n".join(lines)
            else:
                detail = "\n".join("%s  %s  rssi=%s" % (n or "?", a, r) for n, a, r in found)
            app.log("info", "manual scan: %d devices, %d matched" % (len(all_dev), len(found)),
                    kind="connection")
            return {"ok": True, "detail": detail}
        return {"ok": True, "detail": "dry-run mode: no scan"}
    if verb == "reconnect":
        app.device.last_error = None
        async def _drop():
            await app.device.disconnect()
        asyncio.run_coroutine_threadsafe(_drop(), app.loop)
        app.set_connected(False)
        return {"ok": True, "detail": "reconnect scheduled"}
    if verb in ("off",):
        return cmd_led(app, ["led", "all", "off"])
    if verb in ("on",):
        return cmd_led(app, ["led", "all", "on"])
    if verb == "led":
        return cmd_led(app, parts)
    if verb == "write":
        return cmd_write(app, parts)
    if verb == "read":
        return cmd_read(app, parts)
    if verb == "setup":
        return cmd_setup(app, parts)
    if verb == "reload":
        app.events, app.macros, app.events_notice = load_events()
        app.log("info", "events.json reloaded", kind="system")
        return {"ok": True, "detail": "events.json reloaded"}
    return {"ok": False, "detail": "unknown command '%s' - try 'help'" % verb}


# ------------------------------------------------------------------------------
# hook install (`setup`)
# ------------------------------------------------------------------------------
def detect_agents():
    out = []
    probes = {
        "claude":   (lambda: bool(shutil.which("claude")) or os.path.exists(home_path(".claude", "settings.json"))),
        "codex":    (lambda: bool(shutil.which("codex")) or os.path.isdir(home_path(".codex"))),
        "cursor":   (lambda: os.path.isdir(home_path(".cursor"))),
        "copilot":  (lambda: bool(shutil.which("copilot")) or os.path.isdir(home_path(".copilot")) or os.path.exists(home_path(".github", "copilot"))),
        "opencode": (lambda: bool(shutil.which("opencode")) or os.path.isdir(home_path(".config", "opencode"))),
        "vscode":   (lambda: os.path.isdir(home_path(".vscode", "extensions"))),
    }
    for a, probe in probes.items():
        try:
            if probe():
                out.append(a)
        except Exception:
            pass
    return out


def home_path(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def hook_cmd(agent):
    py = shutil.which("python") or shutil.which("python3") or "python"
    script = os.path.join(SCRIPT_DIR, "agent_hook.py")
    return '%s %s --agent %s' % (py, script, agent)


def write_agent_hooks(app, agent):
    cmd = hook_cmd(agent)
    if agent == "claude":
        return _merge_events_json(home_path(".claude", "settings.json"), "hooks",
                                  CLAUDE_HOOK_EVENTS, cmd)
    if agent == "codex":
        return _merge_events_json(home_path(".codex", "config.json"), "hooks",
                                  CODEX_HOOK_EVENTS, cmd)
    if agent == "copilot":
        return _merge_copilot_hooks()
    if agent == "cursor":
        return _merge_cursor(home_path(".cursor", "hooks.json"), CURSOR_HOOK_EVENTS, cmd)
    if agent == "opencode":
        return _install_opencode_plugin()
    if agent == "vscode":
        return _install_vscode_extension()
    return False, "setup for '%s' not implemented; run agent_hook.py manually via your agent's hook settings" % agent


def _merge_events_json(path, key, events, cmd):
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False, "existing file is not valid JSON: %s" % path
    if not isinstance(data, dict):
        return False, "existing file is not a JSON object: %s" % path
    section = data.get(key)
    if not isinstance(section, dict):
        section = {}
    hook_obj = {"type": "command", "command": cmd}
    for ev in events:
        entries = section.get(ev)
        if not isinstance(entries, list):
            entries = []
        found = any(
            isinstance(entry, dict)
            and any(isinstance(h, dict) and h.get("type") == "command" and h.get("command") == cmd
                    for h in entry.get("hooks", [])) if isinstance(entry.get("hooks"), list) else False
            for entry in entries)
        if not found:
            entries.append({"matcher": "", "hooks": [dict(hook_obj)]})
            section[ev] = entries
    data[key] = section
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        return False, "write failed: %s" % e
    return True, "updated %s" % path


def _merge_cursor(path, events, cmd):
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False, "existing file is not valid JSON: %s" % path
    if not isinstance(data, dict):
        return False, "existing file is not a JSON object: %s" % path
    for ev in events:
        data[ev] = cmd
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        return False, "write failed: %s" % e
    return True, "updated %s" % path


def _merge_copilot_hooks():
    """Write personal Copilot CLI hooks to ~/.copilot/hooks/*.json (current
    documented format). Each hook feeds the agent payload (on stdin) to
    agent_hook.py. Merge is idempotent and preserves other hooks."""
    path = home_path(".copilot", "hooks", "sn902-status.json")
    script = os.path.join(SCRIPT_DIR, "agent_hook.py")
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False, "existing file is not valid JSON: %s" % path
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for ev in COPILOT_HOOK_EVENTS:
        entries = hooks.get(ev)
        if not isinstance(entries, list):
            entries = []
        ps = "python '%s' %s --agent copilot" % (script, ev)
        if not any(isinstance(h, dict) and h.get("powershell") == ps for h in entries):
            entries.append({
                "type": "command",
                "bash": "python3 '%s' %s --agent copilot" % (script, ev),
                "powershell": ps,
                "timeoutSec": 5,
            })
            hooks[ev] = entries
    data["hooks"] = hooks
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        return False, "write failed: %s" % e
    return True, ("updated %s (Copilot CLI reads ~/.copilot/hooks/*.json; "
                  "no 'await' state - approval prompts are interactive UI)") % path


def _install_opencode_plugin():
    """Copy the opencode plugin into the global plugins dir (auto-discovered)."""
    src = os.path.join(SCRIPT_DIR, "sn902.opencode.plugin.js")
    if not os.path.exists(src):
        return False, "plugin source missing: %s" % src
    dest_dir = home_path(".config", "opencode", "plugins")
    dest = os.path.join(dest_dir, "sn902.js")
    try:
        os.makedirs(dest_dir, exist_ok=True)
        with open(src, "rb") as f:
            content = f.read()
        tmp = dest + ".tmp"
        with open(tmp, "wb") as f:
            f.write(content)
        os.replace(tmp, dest)
    except Exception as e:
        return False, "write failed: %s" % e
    return True, ("installed %s (OPTIONAL: only works on opencode builds with the "
                  "legacy 'event' plugin hook; on Desktop >=1.18 the built-in "
                  "opencode.db watcher is used instead)") % dest


def _install_vscode_extension():
    """Copy the VS Code Copilot-status extension into ~/.vscode/extensions/.

    VS Code auto-discovers folders there named <publisher>.<name>-<version>.
    After installing, reload the VS Code window (or restart VS Code) and open
    the "SN902" output channel to confirm which Copilot signal is live.
    """
    src_dir = os.path.join(SCRIPT_DIR, "vscode-copilot")
    manifest = os.path.join(src_dir, "package.json")
    if not (os.path.isdir(src_dir) and os.path.exists(manifest)):
        return False, "extension source missing: %s" % src_dir
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            meta = json.load(f)
        name = "%s.%s-%s" % (meta.get("publisher", "sleepace-local"),
                             meta.get("name", "sn902-copilot-status"),
                             meta.get("version", "0.1.0"))
    except Exception as e:
        return False, "could not read extension manifest: %s" % e
    dest_dir = os.path.join(home_path(".vscode", "extensions"), name)
    try:
        os.makedirs(dest_dir, exist_ok=True)
        for fn in os.listdir(src_dir):
            if fn.startswith("."):
                continue
            src = os.path.join(src_dir, fn)
            if os.path.isfile(src):
                tmp = os.path.join(dest_dir, fn + ".tmp")
                with open(src, "rb") as f:
                    content = f.read()
                with open(tmp, "wb") as f:
                    f.write(content)
                os.replace(tmp, os.path.join(dest_dir, fn))
    except Exception as e:
        return False, "write failed: %s" % e
    return True, ("installed %s (reload VS Code; then check the 'SN902' output "
                  "channel; the light responds to Chat start/complete/error)") % dest_dir


# ------------------------------------------------------------------------------
# HTTP web console
# ------------------------------------------------------------------------------
def start_http(app, port):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _html(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                p = os.path.join(SCRIPT_DIR, "webui.html")
                try:
                    with open(p, "rb") as f:
                        self._html(f.read())
                except FileNotFoundError:
                    self._html(b"webui.html missing")
                return
            if path == "/api/state":
                self._json(app.state_dict())
                return
            if path == "/api/events":
                q = parse_qs(parsed.query)
                since = 0
                try:
                    since = int(q.get("since", ["0"])[0])
                except ValueError:
                    pass
                evs = app.events_since(since)
                self._json({"ok": True, "events": evs, "now": app.event_id})
                return
            if path == "/api/help":
                self._json({"ok": True, "detail": HELP_TEXT % (app.config.get("web_port"), ", ".join(COLORS))})
                return
            if path == "/favicon.ico":
                self.send_response(404)
                self.end_headers()
                return
            self._json({"ok": False, "detail": "not found"}, 404)

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b""
            except Exception:
                body = b""
            data = {}
            if body:
                try:
                    data = json.loads(body.decode("utf-8", "replace"))
                except Exception:
                    data = {}
            if path == "/api/cmd":
                cmd = str(data.get("cmd") or "")
                result = execute_cmd(app, cmd, source="console")
                self._json(result)
                return
            if path == "/api/setup":
                result = cmd_setup(app, ["setup"] + [str(data.get("agent") or "all")])
                self._json(result)
                return
            self._json({"ok": False, "detail": "not found"}, 404)

    httpd = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    app.log("info", "web console on http://127.0.0.1:%s" % port)
    httpd.serve_forever()


# ------------------------------------------------------------------------------
# entry point
# ------------------------------------------------------------------------------
def parse_args():
    ap = argparse.ArgumentParser(description="SN902W status-light daemon")
    ap.add_argument("--config", help="path to config.json")
    ap.add_argument("--web-port", type=int, help="web console port (default 7800)")
    ap.add_argument("--hook-port", type=int, help="hook TCP port (default 47800)")
    ap.add_argument("--address", help="BLE MAC address (skips scanning)")
    ap.add_argument("--dry-run", action="store_true", help="run without a physical device")
    ap.add_argument("--scan", action="store_true", help="scan for devices and exit")
    ap.add_argument("--setup", nargs="?", const="all", help="write agent hooks and exit")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    ap.add_argument("--verbose", action="store_true", help="debug logging")
    return ap.parse_args()


def _crash_log(exc):
    try:
        with open(os.path.join(SCRIPT_DIR, "webserver-crash.log"), "a", encoding="utf-8") as f:
            f.write("\n[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), traceback.format_exc()))
    except Exception:
        pass


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    if args.scan:
        async def _s():
            found, all_dev, adapter_ok, adapter_error = await sn902.SN902Device.scan(timeout=10)
            if not adapter_ok:
                print("No Bluetooth adapter found (%s)" % adapter_error)
                print("Plug in / enable a Bluetooth adapter, then retry.")
                return
            print("%d device(s) on the air:" % len(all_dev))
            for n, a, r, md, sv in all_dev:
                mark = "*"
                if not (n and any(f in n for f in sn902.NAME_FRAGMENTS)):
                    mark = "*" if any(u.lower().startswith(s) for s in sn902.MATCH_SERVICES for u in (sv or [])) else " "
                extra = (" svc=" + ",".join(sv)) if sv else ""
                print("%s %-26s %s  rssi=%s%s" % (mark, n or "(no name)", a, r, extra))
            if found:
                print("\nSN902 matches: " + ", ".join(n or a for n, a, _r in found))
            else:
                print("\nNo SN902 match found.")
        asyncio.run(_s())
        return

    cfg = load_config(args)

    if args.setup:
        app = App(cfg, loop=None, dry_run=True)
        result = cmd_setup(app, ["setup", args.setup])
        print(result["detail"])
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _loop_exc_handler(loop_, context):
        _crash_log(context.get("exception"))
        log.error("asyncio error: %s", context)

    loop.set_exception_handler(_loop_exc_handler)
    threading.excepthook = lambda a: _crash_log(a.exc_value)

    app = App(cfg, loop, dry_run=args.dry_run)

    loop.create_task(ble_worker(app))
    loop.create_task(hook_server(app))

    http_thread = threading.Thread(target=start_http, args=(app, cfg["web_port"]), daemon=True)
    http_thread.start()

    if cfg.get("opencode_watch", True):
        watch_thread = threading.Thread(target=opencode_watcher_thread, args=(app,), daemon=True)
        watch_thread.start()

    url = "http://127.0.0.1:%s" % cfg["web_port"]
    print("=" * 62)
    print("  SN902W status light daemon")
    print("  web console  ->  %s" % url)
    print("  hook server  ->  127.0.0.1:%s" % cfg["hook_port"])
    print("  mode         ->  %s" % ("DRY-RUN (no device)" if args.dry_run else "live"))
    print("  send a test command in the console:  led all blink")
    print("=" * 62)

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    except Exception:
        _crash_log(Exception())
        raise
    finally:
        try:
            asyncio.run_coroutine_threadsafe(app.device.disconnect(), loop).result(timeout=3)
        except Exception:
            pass
        print("\nstopped")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
