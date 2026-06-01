/* Terminal panel — xterm.js attached to /ws/terminal/{session_id}.
 *
 * Lifecycle:
 *   1) User clicks #sidebar-terminal-btn → openTerminal() opens the modal.
 *   2) If no current session, POST /api/terminal/spawn to get a session_id.
 *   3) Construct xterm.js Terminal, attach FitAddon, open in #terminal-container.
 *   4) Open WebSocket; pipe keystrokes → ws.send; ws.onmessage → term.write.
 *   5) Send {"type":"resize",cols,rows} on terminal resize.
 *   6) Close modal → detach WS (pty stays alive on server).
 *
 * Session persistence: the active session_id is remembered in
 * sessionStorage so reopening the modal re-attaches instead of spawning.
 */

const SESSION_KEY = 'odysseus_terminal_session_id';
const SHELL_KEY = 'odysseus_terminal_shell_key';

let term = null;          // xterm.js Terminal instance (lazy-created)
let fitAddon = null;
let canvasAddon = null;
let ws = null;            // active WebSocket
let currentSessionId = null;
let resizeObserver = null;
let availableShells = [];

function $(id) { return document.getElementById(id); }

function setSessionInfo(text) {
  const el = $('terminal-session-info');
  if (el) el.textContent = text || '';
}

async function spawnSession(cols, rows) {
  const shellKey = localStorage.getItem(SHELL_KEY) || '';
  const shellEntry = availableShells.find((s) => s.key === shellKey);
  const body = { cols, rows };
  if (shellEntry) {
    body.shell = shellEntry.command;
    if (shellEntry.default_cwd) body.cwd = shellEntry.default_cwd;
  }
  const resp = await fetch('/api/terminal/spawn', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error('spawn failed: ' + resp.status);
  return await resp.json();
}

async function loadShells() {
  try {
    const r = await fetch('/api/terminal/shells', { credentials: 'include' });
    if (!r.ok) return;
    const j = await r.json();
    availableShells = j.shells || [];
  } catch { availableShells = []; }
  const picker = $('terminal-shell-picker');
  if (!picker) return;
  picker.innerHTML = '';
  const saved = localStorage.getItem(SHELL_KEY) || (availableShells[0] && availableShells[0].key) || '';
  availableShells.forEach((s) => {
    const opt = document.createElement('option');
    opt.value = s.key;
    opt.textContent = s.label;
    if (s.key === saved) opt.selected = true;
    picker.appendChild(opt);
  });
  if (saved && !availableShells.find((s) => s.key === saved)) {
    // Saved choice no longer available — fall back to first.
    localStorage.setItem(SHELL_KEY, availableShells[0] ? availableShells[0].key : '');
  }
  picker.onchange = () => { localStorage.setItem(SHELL_KEY, picker.value); };
}

async function listSessions() {
  try {
    const r = await fetch('/api/terminal/list', { credentials: 'include' });
    if (!r.ok) return [];
    const j = await r.json();
    return j.sessions || [];
  } catch { return []; }
}

async function killSession(sid) {
  try {
    await fetch(`/api/terminal/${sid}/kill`, { method: 'POST', credentials: 'include' });
  } catch {}
}

function ensureTerm() {
  if (term) return term;
  if (typeof Terminal === 'undefined') {
    console.error('[terminal] xterm.js not loaded yet');
    return null;
  }
  term = new Terminal({
    // Font stack order: Cascadia (Windows Terminal default — best CJK + box
    // chars), then JetBrains Mono / Fira Code (excellent ligature + powerline
    // glyph coverage), then system mono. Segoe UI Symbol / Symbol Mono are
    // last-ditch fallbacks for stray emoji / arrows that printed as tofu.
    fontFamily: '"Cascadia Code", "Cascadia Mono", "JetBrains Mono", "Fira Code", "Source Code Pro", Consolas, "DejaVu Sans Mono", "Liberation Mono", "Segoe UI Symbol", "Segoe UI Emoji", ui-monospace, monospace',
    fontSize: 13,
    cursorBlink: true,
    cursorStyle: 'block',
    scrollback: 5000,
    allowProposedApi: true,
    // BEL (\x07) from the pty would otherwise audibly beep. Browser-tab
    // bells are universally hated; mute them.
    bellStyle: 'none',
    theme: {
      background: '#000000',
      foreground: '#e6e6e6',
      cursor: '#e6e6e6',
      selectionBackground: '#3a3a3a',
    },
  });
  if (typeof FitAddon !== 'undefined' && FitAddon.FitAddon) {
    fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
  }
  // Unicode11 addon — improves wide-char (CJK, emoji, braille) measurement.
  if (typeof Unicode11Addon !== 'undefined' && Unicode11Addon.Unicode11Addon) {
    try {
      const u = new Unicode11Addon.Unicode11Addon();
      term.loadAddon(u);
      term.unicode.activeVersion = '11';
    } catch (e) { console.warn('[terminal] unicode11 load failed', e); }
  }
  term.open($('terminal-container'));
  // Canvas renderer addon — has to load AFTER term.open(). Massively
  // improves fidelity of complex glyphs vs the default DOM renderer
  // (e.g. claude CLI mascot's braille pixels).
  if (typeof CanvasAddon !== 'undefined' && CanvasAddon.CanvasAddon) {
    try {
      canvasAddon = new CanvasAddon.CanvasAddon();
      term.loadAddon(canvasAddon);
    } catch (e) { console.warn('[terminal] canvas addon load failed', e); }
  }
  // Keystrokes → server
  term.onData((data) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(data);
    }
  });
  // Local resize → server
  term.onResize(({ cols, rows }) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: 'resize', cols, rows })); } catch {}
    }
  });
  return term;
}

function fitNow() {
  if (!fitAddon || !term) return;
  try {
    fitAddon.fit();
    // Push the new size to the server so the pty matches.
    if (ws && ws.readyState === WebSocket.OPEN) {
      const { cols, rows } = term;
      try { ws.send(JSON.stringify({ type: 'resize', cols, rows })); } catch {}
    }
  } catch {}
}

// Several fits over the next few hundred ms — the panel's flex/grid
// dimensions don't settle until after the next layout pass, and fonts may
// still be loading. One fit isn't enough; spam it briefly.
function fitSoon() {
  requestAnimationFrame(fitNow);
  setTimeout(fitNow, 60);
  setTimeout(fitNow, 200);
  setTimeout(fitNow, 500);
}

function attachWebSocket(sessionId) {
  if (ws) {
    try { ws.close(); } catch {}
    ws = null;
  }
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${window.location.host}/ws/terminal/${sessionId}`;
  ws = new WebSocket(url);
  ws.onopen = () => {
    // Server pty was created with our initial cols/rows. After attach,
    // run another fit + resize burst — by now the panel layout has settled
    // and we know the true terminal size.
    fitSoon();
  };
  ws.onmessage = (ev) => {
    if (term) term.write(typeof ev.data === 'string' ? ev.data : '');
  };
  ws.onclose = () => {
    if (term) term.write('\r\n\x1b[2m[disconnected]\x1b[0m\r\n');
    // Forget session if it's gone on the server side; user can spawn a new one.
    // We DON'T clear sessionStorage on every disconnect because reload should re-attach.
  };
  ws.onerror = (e) => {
    console.warn('[terminal] ws error', e);
  };
}

async function openTerminal() {
  const panel = $('terminal-panel');
  if (!panel) return;
  // If the panel is already visible AND we have a live WebSocket, this is
  // a re-click of the sidebar Terminal button while already on the terminal
  // screen. Don't tear down + reattach — that just bounces the pty for no
  // user-visible reason (and sometimes emits a stray BEL during reattach).
  // Just refocus the existing terminal and return.
  const alreadyVisible = !panel.classList.contains('hidden');
  const wsLive = ws && ws.readyState === WebSocket.OPEN;
  if (alreadyVisible && wsLive && term) {
    try { term.focus(); } catch {}
    return;
  }
  panel.classList.remove('hidden');

  ensureTerm();
  // Trigger several fits to handle layout / font load races.
  fitSoon();

  // Make sure the shells list is loaded before we try to spawn — otherwise
  // the user's pref (e.g. pwsh) silently falls back to default bash.
  if (availableShells.length === 0) {
    try { await loadShells(); } catch {}
  }

  // Decide which session to attach to: stored session if still alive on the
  // server, otherwise spawn a fresh one.
  let sessionId = sessionStorage.getItem(SESSION_KEY);
  if (sessionId) {
    const sessions = await listSessions();
    if (!sessions.find((s) => s.session_id === sessionId)) sessionId = null;
  }
  if (!sessionId) {
    const cols = (term && term.cols) || 100;
    const rows = (term && term.rows) || 30;
    try {
      const spawned = await spawnSession(cols, rows);
      sessionId = spawned.session_id;
      sessionStorage.setItem(SESSION_KEY, sessionId);
    } catch (e) {
      if (term) term.write(`\r\n\x1b[31m[spawn failed: ${e.message}]\x1b[0m\r\n`);
      return;
    }
  }
  currentSessionId = sessionId;
  setSessionInfo(`${sessionId.slice(0, 8)} • attaching…`);
  attachWebSocket(sessionId);
  // Refresh meta with real pid + shell once we have it.
  listSessions().then((sessions) => {
    const me = sessions.find((s) => s.session_id === sessionId);
    if (me) {
      const shellShort = me.shell.split(/[\\/]/).pop();
      setSessionInfo(`${sessionId.slice(0, 8)} • ${shellShort} • pid ${me.pid}`);
    }
  });

  // Watch container size → refit.
  if (!resizeObserver) {
    resizeObserver = new ResizeObserver(() => fitNow());
  }
  const container = $('terminal-container');
  if (container) {
    try { resizeObserver.disconnect(); } catch {}
    resizeObserver.observe(container);
  }
  // Focus so the user can type immediately.
  setTimeout(() => { try { term && term.focus(); } catch {} }, 50);
}

function closeTerminal() {
  const modal = $('terminal-panel');
  if (modal) modal.classList.add('hidden');
  // Detach WS but DO NOT kill the pty — it lives on for next open.
  if (ws) {
    try { ws.close(); } catch {}
    ws = null;
  }
  if (resizeObserver) {
    try { resizeObserver.disconnect(); } catch {}
  }
}

async function killCurrent() {
  if (!currentSessionId) return;
  await killSession(currentSessionId);
  sessionStorage.removeItem(SESSION_KEY);
  currentSessionId = null;
  if (ws) { try { ws.close(); } catch {}; ws = null; }
  if (term) term.write('\r\n\x1b[2m[killed]\x1b[0m\r\n');
  setSessionInfo('');
}

async function newSession() {
  // Optional: keep the previous session alive in the background (the user
  // can `claude --resume` it next time). For now, we just leave it running
  // and switch to a fresh one. If the user wants to clean up, they Kill.
  sessionStorage.removeItem(SESSION_KEY);
  currentSessionId = null;
  if (ws) { try { ws.close(); } catch {}; ws = null; }
  if (term) {
    term.reset();
  }
  await openTerminal();
}

function wireUp() {
  const open = $('sidebar-terminal-btn');
  if (open) open.addEventListener('click', openTerminal);
  // Preload shells list so the dropdown is populated on first open.
  loadShells();
  const close = $('close-terminal-panel');
  if (close) close.addEventListener('click', closeTerminal);
  const kill = $('terminal-kill-btn');
  if (kill) kill.addEventListener('click', killCurrent);
  const newBtn = $('terminal-new-btn');
  if (newBtn) newBtn.addEventListener('click', newSession);

  // Close on Esc when modal is open.
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const modal = $('terminal-panel');
      if (modal && !modal.classList.contains('hidden')) closeTerminal();
    }
  });

  // Window resize → refit if open.
  window.addEventListener('resize', () => {
    const modal = $('terminal-panel');
    if (modal && !modal.classList.contains('hidden')) fitNow();
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', wireUp);
} else {
  wireUp();
}
