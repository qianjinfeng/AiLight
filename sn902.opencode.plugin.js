// sn902.opencode.plugin.js
//
// opencode plugin that reports agent events to the local SN902 status-light
// daemon (webserver.py) over TCP. Installed by `setup opencode` (or
// `setup all`) into ~/.config/opencode/plugins/sn902.js, where opencode
// auto-discovers it. Restart opencode after installing.
//
// It subscribes to opencode's bus events (the `event` plugin hook), maps the
// dot-style bus event names onto the canonical event names used by
// agent_hook.py / events.json, and forwards them to the daemon on port 47800
// (override with SN902_HOOK_PORT). The daemon then drives the light.

import { connect } from 'node:net';

const HOST = process.env.SN902_HOOK_HOST || '127.0.0.1';
const PORT = Number(process.env.SN902_HOOK_PORT || '47800');

// opencode bus event -> canonical event name (same table as events.json).
// Unknown/noisy events are ignored.
const EVENT_MAP = {
  'session.initialized': 'SessionStart',
  'session.idle': 'Stop',
  'session.error': 'StopFailure',
  'user.prompt.submit': 'UserPromptSubmit',
  'tool.execute.before': 'PreToolUse',
  'tool.execute.after': 'PostToolUse',
  'tool.execute.error': 'PostToolUseFailure',
  'permission.ask': 'PermissionRequest',
  'session.paused': 'Stop',
};

function firstProp(props, keys) {
  for (const k of keys) {
    const v = props && props[k];
    if (v != null && v !== '') return String(v);
  }
  return '';
}

function report(evt, props) {
  const mapped = EVENT_MAP[evt];
  if (!mapped) return; // ignore noise events (message.part.updated etc.)
  const tool = firstProp(props, ['tool', 'toolName', 'name']);
  const detail = firstProp(props, ['prompt', 'command', 'description', 'error']);
  const parts = [evt];
  if (tool) parts.push('tool=' + tool);
  if (detail) parts.push(String(detail).slice(0, 120));
  const payload = {
    cmd: '',
    event: mapped,
    message: parts.join('  '),
    session: firstProp(props, ['sessionID', 'session_id']) || undefined,
    cwd: firstProp(props, ['cwd', 'directory']) || undefined,
    agent: 'opencode',
  };
  const sock = connect(PORT, HOST);
  sock.setTimeout(1000, () => {
    try { sock.destroy(); } catch (_) { /* ignore */ }
  });
  sock.on('connect', () => {
    try { sock.end(JSON.stringify(payload) + '\n'); } catch (_) { /* ignore */ }
  });
  sock.on('error', () => { /* daemon may be down - never break opencode */ });
}

export default async function () {
  return {
    event: (input) => {
      try {
        if (!input || !input.event) return;
        const props = input.properties || {};
        report(String(input.event), props);
      } catch (_) {
        // never break opencode
      }
    },
  };
}
