# SN902W Status Light (PromLight-style)

Turn a **Sleepace SN902W** ("Nox 902W") into a Bluetooth status light that
mirrors your AI agent's state — working / waiting / done / error — using the
same hook architecture as [PromLight](https://github.com/mediapig/promlight).

The Sleepace app 3.8.11 (`com.medica.xiangshui`) BLE protocol is
reverse-engineered; the device is driven directly over BLE (no phone app
needed).

## Architecture

```
 Claude Code / Codex / Cursor / Copilot / opencode ...
        |   (Hook event JSON on stdin)
        v
 agent_hook.py ──TCP JSON (127.0.0.1:47800)──▶ webserver.py (daemon)
                                              │  BLE GATT ──▶ SN902W LED
        user browser ──HTTP (127.0.0.1:7800)──┘  (web console + API)
```

| File | Purpose |
|------|---------|
| `webserver.py` | daemon: BLE connection, hook TCP server (47800), web console (7800), light-effect engine, `setup` command |
| `sn902.py` | SN902W BLE protocol (Nox2Packet framing + light commands) |
| `agent_hook.py` | agent hook client; maps agent events to light commands, sends them to the daemon |
| `sn902.opencode.plugin.js` | opencode plugin source (installed by `setup opencode`) |
| `vscode-copilot/` | VS Code extension source (installed by `setup vscode`) |
| `webui.html` | web console page served by the daemon |
| `config.json` | daemon config (BLE address, ports, brightness...) |
| `events.json` | event → light-command mapping (edit freely, hot-reload with `reload`) |
| `devices.json` | device alias / routing placeholder (single device) |
| `install.ps1` / `uninstall.ps1` | install deps + optional startup registration |

## Quick start

1. **Pair the device.** Power on the Nox 902W, put it in Bluetooth pairing
   range of this PC. (BLE GATT connection is used for control; classic BT
   pairing is not required.)
2. **Install dependencies**
   ```powershell
   pip install -r requirements.txt      # or: .\install.ps1
   ```
3. **Run the daemon**
   ```powershell
   python webserver.py
   ```
   It scans for the device (`SN22…`/`SN-21…`/`Nox 902…`), connects over BLE,
   and opens the web console at `http://127.0.0.1:7800`.
   - No device handy? `python webserver.py --dry-run` simulates everything.
   - Pin a specific device: set `"ble_address"` in `config.json`, or
     `python webserver.py --address D4:6C:50:xx:xx:xx`.
4. **Test the light.** In the web console send:
   ```
   led all blink
   led green on --only --fade 800
   ```

## Connect an AI agent

Run in the web console (or `python webserver.py --setup <agent>`):

```
setup all          # detects + configures every installed agent
setup claude
setup codex
setup cursor
setup copilot      # Copilot CLI (terminal) - writes ~/.copilot/hooks/
setup opencode     # installs sn902.js into ~/.config/opencode/plugins/
setup vscode       # installs the VS Code Copilot-status extension
```

- **Claude Code / Codex / Cursor / Copilot CLI**: hook config files are merged
  (`~/.claude/settings.json`, `~/.codex/config.json`,
  `~/.cursor/hooks.json`, `~/.copilot/hooks/sn902-status.json`). Start a new
  conversation to activate.

  Copilot CLI caveat — **it has no "waiting for user" signal.** Its approval
  prompts are interactive UI, and its native hooks
  (`sessionStart`, `userPromptSubmitted`, `preToolUse`, `postToolUse`,
  `agentStop`, `sessionEnd`, `errorOccurred`) contain no permission event.
  `preToolUse` is deliberately not mapped: it is a blocking hook that also
  fires on every auto-approved tool call, so it cannot mean "waiting for you".
- **opencode**: built in — no install step. OpenCode Desktop ≥1.18 uses an
  Effect-based plugin API that has **no session-event hook**, so the daemon
  instead tails the local `opencode.db` event table
  (`~/.local/share/opencode/opencode.db`) and turns recent agent activity into
  light states (`opencode.busy`→yellow, `opencode.idle`→green,
  `opencode.error`→red). Tune with `opencode_watch`, `opencode_db`,
  `opencode_idle_ms` in `config.json`. (`setup opencode` still installs
  `~/.config/opencode/plugins/sn902.js`, which only works on opencode builds
  that still support the legacy `event` hook; it is optional.)
- **VS Code Copilot**: the extension goes into
  `~/.vscode/extensions/sleepace-local.sn902-copilot-status-0.1.0/`. **Reload
  the VS Code window** after installing, then open the **"SN902"** output
  channel to confirm which signal is active. Copilot exposes no official
  status hooks, so the extension uses the Copilot Chat participant API
  (`github.copilot-chat`) for Chat start/complete/error, plus the core
  `github.copilot` status signal for inline completions — each guarded so a
  changed API just disables that signal instead of breaking VS Code. Manual
  test: Command Palette → **SN902: Test light (send Copilot start)**.

## Default light mapping (edit `events.json`)

| State | Light |
|-------|-------|
| session / new task start | cyan blink x2, then green |
| working (prompt/tool/subagent) | solid yellow |
| waiting for permission / user | yellow blink |
| error | red blink x5 |
| done / idle | solid green |
| session end | green breathing (long) |

## Web console commands

```
help
devices                                  connection state
scan                                     rescan BLE
reconnect                                force reconnect
led <color> <action> [opts]              on | off | blink | breath
    color: green red yellow orange cyan blue magenta purple pink
           white  all  off  #rrggbb
    opts:  --only  --count N  --freq N|Nms|Ns  --fade N|Nms|Ns
write led.brightness <1-100>             default brightness
read led.brightness
setup <agent|all>
reload                                   reload events.json
```

## SN902W BLE protocol (reverse-engineered from Sleepace 3.8.11)

- **GATT**: write service `0000ffe5-…`, write char `0000ffe9-…`,
  notify service `0000ffe0-…`, notify char `0000ffe4-…`.
- **Frame**: `[ver=0][type=2][btCount=1][btIndex=0][seq][devType=11(be)][msgType][content][crc32][24 5F 40 2D]`
- **Light (msgType 0x30)**: `[op|ctrl<<4][brightness][mode][r][g][b][w]`
  with `op` 1=open 0=close 2=brightness, `ctrl=1`, `mode` 0=white 1=RGB.
- See `sn902.py` for the full implementation.

## Device notes (Nox 902B / 902W)

- The devices **advertise with their SN number** in the BLE name (the units we
  tested showed short numeric names such as "1433"/"0444") and expose signature
  services `0000fff0`/`0000ffb0` in the advertisement — both are used for
  auto-detection (plus you can pin MACs in `config.json`).
- They support **two Bluetooth modes**: Classic (BT audio) and **BLE** (control).
  This project drives the light over **BLE** only.
- A Nox has a **single RGBW LED**, so instead of PromLight's three separate
  red/yellow/green LEDs, this project uses **one light with different colors**
  to encode status (yellow=working, yellow-blink=awaiting, green=idle,
  red-blink=error, cyan=start, green-breath=end).

## Notes

- The light engine emulates fade/breath by ramping brightness over BLE, so it
  needs the BLE link to be up; if the device goes out of range the daemon
  reconnects and re-applies the last effect.
- Hooks only send status events locally — no prompts/file contents leave the
  machine, nothing goes to the cloud.

## Troubleshooting: "no Bluetooth adapter found"

The daemon needs a working Bluetooth 4.0+ radio on the PC where you use the AI.
If the web console shows **"no bluetooth adapter"** (or `scan` returns
"no Bluetooth adapter found"), the PC has no usable radio:

- **Desktop PCs** often have no Bluetooth hardware at all. Plug in a **USB
  Bluetooth 4.0/5.0 dongle** (or an M.2 WiFi+BT card) and Windows will pick it
  up — the daemon reconnects automatically.
- If a built-in adapter shows as a *phantom* (Device Manager, Code 45
  "device is not connected"), it is not physically present/enabled — enable it
  in the BIOS (WiFi/BT module) or reconnect the module.

Still not found after Bluetooth is working?

- The Nox 902W advertises only briefly after power-on and may sleep. Power it
  on and keep it within ~5 m of the PC.
- If Windows has already **paired/bonded** the device, some Bluetooth stacks
  hide it from scans. Put its MAC into `config.json`
  (`"ble_address": "xx:xx:xx:xx:xx:xx"`) to connect directly — or unpair it in
  Windows Bluetooth settings and let the daemon pair fresh.
- Scan shows the name/address? Run `scan` in the web console and match the
  advertised name against `"name_fragments"` in `config.json`.
