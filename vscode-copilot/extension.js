// SN902 Copilot Status - VS Code extension
//
// Watches GitHub Copilot activity in VS Code and reports it to the local
// SN902W status-light daemon (webserver.py) over TCP, using the same JSON
// hook protocol as agent_hook.py. The daemon maps the event names below via
// events.json and drives the light.
//
// Copilot (VS Code) does NOT expose official "working/waiting/done" hooks the
// way Claude Code does. This extension therefore uses the two best available
// signals, each guarded so a missing/changed API never breaks VS Code:
//
//   1. github.copilot-chat  - the chat participant's onDidPerformAction /
//      chat-level onDidPerformAction events (start / complete / error).
//   2. github.copilot       - the core extension's status signal
//      (InProgress / Done / Error), mainly inline completions.
//
// Every failure is logged to an Output channel ("SN902") so you can verify
// which signal is live on your setup.

const vscode = require('vscode');
const net = require('net');

const DEFAULT_PORT = 47800;

// event name -> mapped by events.json in the daemon
const EV = {
  chatStart:   'copilot.chat.start',     // -> work (yellow)
  chatRequest: 'copilot.chat.request',   // -> work
  chatProgress:'copilot.chat.progress',  // -> work
  chatComplete:'copilot.chat.complete',  // -> idle (green)
  chatError:   'copilot.chat.error',     // -> error (red blink)
  suggestWork: 'copilot.suggest.work',   // inline completion started
  suggestIdle: 'copilot.suggest.idle',   // inline completion done
  suggestError:'copilot.suggest.error',  // inline completion error
};

let output = null;

function log(msg) {
  try {
    if (!output) {
      output = vscode.window.createOutputChannel('SN902');
    }
    output.appendLine('[' + new Date().toLocaleTimeString() + '] ' + msg);
  } catch (_) { /* ignore */ }
}

function getPort() {
  try {
    const cfg = vscode.workspace.getConfiguration('sn902');
    const p = cfg.get('hookPort', DEFAULT_PORT);
    if (typeof p === 'number' && p > 0 && p < 65536) return p;
  } catch (_) { /* ignore */ }
  const env = parseInt(process.env.SN902_HOOK_PORT || '', 10);
  if (!isNaN(env) && env > 0 && env < 65536) return env;
  return DEFAULT_PORT;
}

function report(event, message) {
  const payload = {
    cmd: '',
    event: event,
    message: message,
    agent: 'vscode-copilot',
  };
  const port = getPort();
  const sock = net.connect(port, '127.0.0.1');
  sock.setTimeout(500, () => { try { sock.destroy(); } catch (_) {} });
  sock.on('connect', () => {
    try { sock.end(JSON.stringify(payload) + '\n'); } catch (_) {}
  });
  sock.on('error', (e) => log('report error (' + port + '): ' + e.message));
  log('report ' + event + ' -> ' + message);
}

// ---- signal 1: Copilot Chat participant / chat events -----------------------
function mapChatAction(action) {
  if (action === 'start' || action === 'request' || action === 'requesting') {
    return EV.chatStart;
  }
  if (action === 'progress') return EV.chatProgress;
  if (action === 'complete' || action === 'done') return EV.chatComplete;
  if (action === 'error' || action === 'failed') return EV.chatError;
  return null;
}

function tryChatApi() {
  try {
    const ext = vscode.extensions.getExtension('github.copilot-chat');
    if (!ext) { log('github.copilot-chat not installed'); return false; }
    const api = ext.exports;
    const chat = api && (api.chat || api);
    if (!chat) { log('github.copilot-chat exports.chat missing'); return false; }

    let wired = false;

    // participant-level onDidPerformAction (most specific)
    if (typeof chat.getChatParticipant === 'function') {
      try {
        const participant = chat.getChatParticipant('copilot');
        if (participant && typeof participant.onDidPerformAction === 'function') {
          participant.onDidPerformAction((e) => {
            const ev = mapChatAction(e && e.action);
            if (ev) report(ev, 'copilot participant action=' + (e && e.action) +
              (e && e.command ? ' command=' + e.command : ''));
          });
          log('signal: github.copilot-chat participant.onDidPerformAction wired');
          wired = true;
        }
      } catch (e) { log('getChatParticipant failed: ' + e.message); }
    }

    // chat-level onDidPerformAction (some versions expose it)
    if (!wired && typeof chat.onDidPerformAction === 'function') {
      chat.onDidPerformAction((e) => {
        const ev = mapChatAction(e && e.action);
        if (ev) report(ev, 'copilot chat action=' + (e && e.action) +
          (e && e.participantId ? ' participant=' + e.participantId : ''));
      });
      log('signal: github.copilot-chat chat.onDidPerformAction wired');
      wired = true;
    }

    return wired;
  } catch (e) {
    log('tryChatApi error: ' + e.message);
    return false;
  }
}

// ---- signal 2: core Copilot status (inline completions) ----------------------
function mapCoreStatus(s) {
  if (s === 'InProgress' || s === 'Working') return EV.suggestWork;
  if (s === 'Done' || s === 'Ready') return EV.suggestIdle;
  if (s === 'Error' || s === 'Failing') return EV.suggestError;
  return null;
}

function tryCoreStatus() {
  try {
    const ext = vscode.extensions.getExtension('github.copilot');
    if (!ext) { log('github.copilot not installed'); return false; }
    const api = ext.exports;
    const status = api && (api.status || api);
    if (!status) { log('github.copilot exports.status missing'); return false; }

    if (typeof status.onDidChangeStatus === 'function') {
      status.onDidChangeStatus((st) => {
        const s = st && (st.status || st);
        const ev = mapCoreStatus(s);
        if (ev) report(ev, 'copilot status=' + s);
      });
      log('signal: github.copilot status.onDidChangeStatus wired');
      return true;
    }
    if (typeof status.onDidChange === 'function') {
      status.onDidChange((st) => {
        const s = st && st.status;
        const ev = mapCoreStatus(s);
        if (ev) report(ev, 'copilot status=' + s);
      });
      log('signal: github.copilot status.onDidChange wired');
      return true;
    }
    log('github.copilot status signal not available');
    return false;
  } catch (e) {
    log('tryCoreStatus error: ' + e.message);
    return false;
  }
}

// ---- public API --------------------------------------------------------------
function activate(context) {
  log('SN902 Copilot Status activated');

  // wire what we can (each source is optional)
  const chatOk = tryChatApi();
  const statusOk = tryCoreStatus();
  if (!chatOk && !statusOk) {
    log('WARNING: no Copilot signal source found. Is "GitHub Copilot" and/or' +
        ' "GitHub Copilot Chat" installed? After installing, reload the window.');
  }

  // manual test command
  context.subscriptions.push(
    vscode.commands.registerCommand('sn902.copilot.test', () => {
      report(EV.chatStart, 'manual test: Copilot start');
      vscode.window.showInformationMessage('SN902: sent "Copilot start" to the light daemon');
    })
  );

  // status command
  context.subscriptions.push(
    vscode.commands.registerCommand('sn902.copilot.status', () => {
      if (output) { output.show(); } else { vscode.window.showInformationMessage('SN902: no log yet'); }
    })
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
