/* Terminal panel — multi-pane xterm.js attached to /ws/terminal/{session_id}.
 *
 * Architecture:
 *   - Each pane is a TerminalPane instance owning its own xterm.js Terminal,
 *     WebSocket, session_id, GitHub-toggle state, and DOM subtree.
 *   - A MultiPaneTerminal manager tracks the active layout (1 / 2h / 2v / 4)
 *     and which panes are currently visible. Switching layouts shows/hides
 *     panes — it does NOT kill their ptys, so you can flip 2 → 1 → 2 and
 *     resume right where you left off.
 *   - Layout + each visible pane's session_id are persisted to sessionStorage
 *     so a page reload restores both the layout and per-pane sessions.
 *
 * Per-pane session persistence keys:
 *   odysseus_terminal_session_id_<paneId>   — session_id last attached
 *   odysseus_terminal_gh_on_<sessionId>     — GitHub toggle state for that session
 *
 * Panel-level persistence:
 *   odysseus_terminal_layout                — '1' | '2h' | '2v' | '4'
 *
 * Naming: pane IDs are letters 'a' / 'b' / 'c' / 'd' to make logs and
 * sessionStorage keys human-readable and stable. Layout '1' uses only A;
 * '2h' / '2v' use A+B; '4' uses A+B+C+D in a 2x2 grid (row-major).
 */

const SHELL_KEY = 'odysseus_terminal_shell_key';     // shared across panes — pref for the default shell
const LAYOUT_KEY = 'odysseus_terminal_layout';
const SESSION_KEY_PREFIX = 'odysseus_terminal_session_id_';
const GH_TOGGLE_KEY_PREFIX = 'odysseus_terminal_gh_on_';

const LAYOUT_PANE_IDS = {
  '1':  ['a'],
  '2h': ['a', 'b'],
  '2v': ['a', 'b'],
  '4':  ['a', 'b', 'c', 'd'],
};
const PANE_LETTERS = { a: 'A', b: 'B', c: 'C', d: 'D' };

let availableShells = [];
// Remembers whether the sidebar was already collapsed before the terminal
// opened, so closeTerminal() restores the exact prior state.
let _sidebarWasHiddenBeforeTerminal = false;

function $(id) { return document.getElementById(id); }

// ── Shells list (shared across panes, populated once) ──
// The shell picker lives inside each pane's toolbar (cloned from template),
// so we update each picker's <option>s when the shells list arrives.

async function loadShells() {
  try {
    const r = await fetch('/api/terminal/shells', { credentials: 'include' });
    if (!r.ok) return;
    const j = await r.json();
    availableShells = j.shells || [];
  } catch { availableShells = []; }
  // Update every pane's shell picker that's already in the DOM.
  document.querySelectorAll('.terminal-pane-shell-picker').forEach(_populateShellPicker);
}

function _populateShellPicker(picker) {
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

// Read terminal palette from CSS vars defined in style.css :root tokens.
// Themes can override these without touching the JS; the xterm canvas
// always agrees with the Odysseus chrome.
function _readTerminalTheme() {
  const cs = getComputedStyle(document.documentElement);
  const pick = (name, fallback) => {
    const v = cs.getPropertyValue(name).trim();
    return v || fallback;
  };
  return {
    background: pick('--terminal-bg', '#0d0d0d'),
    foreground: pick('--terminal-fg', '#e6e6e6'),
    cursor: pick('--terminal-cursor', '#e6e6e6'),
    selectionBackground: pick('--terminal-selection-bg', 'rgba(255,255,255,0.18)'),
    // ANSI 16-color palette. Without these, colored output (git, ls, claude's
    // TUI) falls back to xterm's generic defaults and looks nothing like the
    // user's editor. Fallbacks below are the One Dark Pro palette (the base of
    // Matthew's "Ayu One Dark Pro" VS Code theme); each is overridable via a
    // CSS var so an Odysseus chrome theme can still retint individual slots.
    black:         pick('--term-ansi-black',   '#2d3139'),
    red:           pick('--term-ansi-red',     '#e06c75'),
    green:         pick('--term-ansi-green',   '#98c379'),
    yellow:        pick('--term-ansi-yellow',  '#d19a66'),
    blue:          pick('--term-ansi-blue',    '#61afef'),
    magenta:       pick('--term-ansi-magenta', '#c678dd'),
    cyan:          pick('--term-ansi-cyan',    '#56b6c2'),
    white:         pick('--term-ansi-white',   '#abb2bf'),
    brightBlack:   pick('--term-ansi-bright-black',   '#5c6370'),
    brightRed:     pick('--term-ansi-bright-red',     '#e06c75'),
    brightGreen:   pick('--term-ansi-bright-green',   '#98c379'),
    brightYellow:  pick('--term-ansi-bright-yellow',  '#e5c07b'),
    brightBlue:    pick('--term-ansi-bright-blue',    '#61afef'),
    brightMagenta: pick('--term-ansi-bright-magenta', '#c678dd'),
    brightCyan:    pick('--term-ansi-bright-cyan',    '#56b6c2'),
    brightWhite:   pick('--term-ansi-bright-white',   '#ffffff'),
  };
}

// ── TerminalPane — owns ONE pty/xterm/WebSocket. ──
// Lifecycle:
//   new TerminalPane('a')      → instantiates with no DOM, no session
//   pane.mount(parentEl)       → injects toolbar+container DOM, wires events
//   pane.open()                → resolves a session (reattach or spawn), connects WS
//   pane.hide() / pane.show()  → toggles DOM visibility WITHOUT killing the pty
//   pane.destroy()             → tears down DOM, closes WS (pty stays alive
//                                on server unless killCurrent() was called)

class TerminalPane {
  constructor(paneId) {
    this.paneId = paneId;
    this.sessionId = null;
    this.term = null;
    this.fitAddon = null;
    this.canvasAddon = null;
    this.ws = null;
    this.intentionalClose = false;
    this.resizeObserver = null;
    this.mounted = false;
    // DOM refs filled in mount():
    this.rootEl = null;       // .terminal-pane wrapper
    this.toolbarEl = null;    // .terminal-pane-toolbar
    this.containerEl = null;  // .terminal-pane-container (where xterm.js paints)
    this.sessionInfoEl = null;
    this.shellPickerEl = null;
    this.newBtnEl = null;
    this.killBtnEl = null;
    this.ghGroupEl = null;
    this.ghIndicatorEl = null;
    this.ghNameEl = null;
    this.ghSettingsEl = null;
  }

  // sessionStorage helpers — keyed by the pane id so each pane remembers
  // its own session across reloads.
  _sessionKey() { return SESSION_KEY_PREFIX + this.paneId; }
  _loadStoredSessionId() {
    try { return sessionStorage.getItem(this._sessionKey()) || null; } catch { return null; }
  }
  _saveStoredSessionId(sid) {
    try {
      if (sid) sessionStorage.setItem(this._sessionKey(), sid);
      else sessionStorage.removeItem(this._sessionKey());
    } catch {}
  }

  // ── DOM ──
  mount(parentEl) {
    if (this.mounted) return;
    const root = document.createElement('div');
    root.className = 'terminal-pane';
    root.dataset.pane = this.paneId;

    // Clone the toolbar template (defined in index.html). The template
    // includes the per-pane GitHub toggle group and shell picker.
    const tpl = $('terminal-pane-toolbar-tpl');
    const toolbar = tpl ? tpl.content.firstElementChild.cloneNode(true) : null;
    if (!toolbar) {
      console.error('[terminal] pane-toolbar template missing');
      return;
    }
    // Stamp the pane letter (A/B/C/D) into the title chip.
    const letterEl = toolbar.querySelector('.terminal-pane-letter');
    if (letterEl) letterEl.textContent = PANE_LETTERS[this.paneId] || '?';

    // Xterm host. min-height:0 lets the flex parent shrink it correctly.
    const container = document.createElement('div');
    container.className = 'terminal-pane-container';

    root.appendChild(toolbar);
    root.appendChild(container);
    parentEl.appendChild(root);

    this.rootEl = root;
    this.toolbarEl = toolbar;
    this.containerEl = container;
    this.sessionInfoEl = toolbar.querySelector('.terminal-pane-session-info');
    this.shellPickerEl = toolbar.querySelector('.terminal-pane-shell-picker');
    this.newBtnEl = toolbar.querySelector('.terminal-pane-new-btn');
    this.killBtnEl = toolbar.querySelector('.terminal-pane-kill-btn');
    this.ghGroupEl = toolbar.querySelector('.terminal-pane-gh-group');
    this.ghIndicatorEl = toolbar.querySelector('.terminal-pane-gh-indicator');
    this.ghNameEl = toolbar.querySelector('.terminal-pane-gh-name');
    this.ghSettingsEl = toolbar.querySelector('.terminal-pane-gh-settings');

    // Populate shell picker (uses the already-loaded shells list).
    _populateShellPicker(this.shellPickerEl);

    // Pane-local action handlers.
    if (this.newBtnEl) this.newBtnEl.onclick = () => this.newSession();
    if (this.killBtnEl) this.killBtnEl.onclick = () => this.killCurrent();

    // Focus this pane on any click inside it — keyboard input goes to the
    // most-recently-clicked pane (the xterm.js instance with DOM focus).
    root.addEventListener('mousedown', () => this.focus(), true);

    this.mounted = true;
  }

  destroy() {
    // Close WS but DO NOT kill the pty (the pty lives on the server until
    // the user explicitly kills it). DOM goes away, but reopening the pane
    // later with the same stored sessionId will reattach to the live pty.
    this.intentionalClose = true;
    if (this.ws) { try { this.ws.close(); } catch {} this.ws = null; }
    if (this.resizeObserver) { try { this.resizeObserver.disconnect(); } catch {} }
    if (this.term) { try { this.term.dispose(); } catch {} this.term = null; }
    if (this.rootEl && this.rootEl.parentNode) {
      this.rootEl.parentNode.removeChild(this.rootEl);
    }
    this.rootEl = null;
    this.toolbarEl = null;
    this.containerEl = null;
    this.fitAddon = null;
    this.canvasAddon = null;
    this.mounted = false;
  }

  hide() {
    if (!this.rootEl) return;
    this.rootEl.style.display = 'none';
  }
  show() {
    if (!this.rootEl) return;
    this.rootEl.style.display = '';
    this.fitSoon();
  }
  focus() {
    // Visual active-pane indicator — clear all others, add to this one.
    document.querySelectorAll('.terminal-pane.is-active').forEach(el => el.classList.remove('is-active'));
    if (this.rootEl) this.rootEl.classList.add('is-active');
    try { this.term && this.term.focus(); } catch {}
  }

  // ── xterm.js ──
  ensureTerm() {
    if (this.term) return this.term;
    if (typeof Terminal === 'undefined') {
      console.error('[terminal] xterm.js not loaded yet');
      return null;
    }
    const term = new Terminal({
      // Font stack: Cascadia (best CJK/box-chars), then JetBrains/Fira (ligatures
      // + powerline), then system mono. Segoe UI Symbol catches stray emoji.
      fontFamily: '"Cascadia Code", "Cascadia Mono", "JetBrains Mono", "Fira Code", "Source Code Pro", Consolas, "DejaVu Sans Mono", "Liberation Mono", "Segoe UI Symbol", "Segoe UI Emoji", ui-monospace, monospace',
      fontSize: 13,
      // Blink OFF: claude's TUI repositions the cursor several times per
      // keystroke as it re-renders the input line; with blink on, xterm flashes
      // the block on/off at each transient position → a "box flashing around"
      // while you type. A steady cursor just follows the text.
      cursorBlink: false,
      cursorStyle: 'block',
      // When the terminal is NOT focused, xterm defaults to drawing the cursor
      // as a hollow outline box. claude's TUI parks/moves the cursor all over
      // the screen while it works, so that outline "flies around" on top of the
      // text whenever you're watching without the terminal focused. 'none' draws
      // nothing while blurred; the normal block cursor still shows when focused.
      cursorInactiveStyle: 'none',
      scrollback: 5000,
      allowProposedApi: true,
      // BEL muting is server-side now (terminal_routes.py:_strip_stray_bels).
      // xterm.js 5.x removed bellStyle entirely; setting it here is a no-op.
      theme: _readTerminalTheme(),
    });
    if (typeof FitAddon !== 'undefined' && FitAddon.FitAddon) {
      this.fitAddon = new FitAddon.FitAddon();
      term.loadAddon(this.fitAddon);
    }
    if (typeof Unicode11Addon !== 'undefined' && Unicode11Addon.Unicode11Addon) {
      try {
        const u = new Unicode11Addon.Unicode11Addon();
        term.loadAddon(u);
        term.unicode.activeVersion = '11';
      } catch (e) { console.warn('[terminal] unicode11 load failed', e); }
    }
    term.open(this.containerEl);
    // Renderer: WebGL. We do NOT use the CanvasAddon (its renderer, deprecated
    // upstream, ghosts the cursor on partial redraws) and we don't want the bare
    // DOM renderer either (it lets a glyph wider than its cell overflow, so
    // claude's animated spinner shifts the whole status line every frame). WebGL
    // draws the cursor correctly AND clips every glyph to its cell, killing both
    // bugs. Reliable here since WebGL runs in the Chrome tab, not WSL. Falls back
    // to the DOM renderer automatically if WebGL can't initialise.
    if (typeof WebglAddon !== 'undefined' && WebglAddon.WebglAddon) {
      try {
        const webgl = new WebglAddon.WebglAddon();
        webgl.onContextLoss(() => { try { webgl.dispose(); } catch {} this.webglAddon = null; });
        term.loadAddon(webgl);
        this.webglAddon = webgl;
      } catch (e) { console.warn('[terminal] webgl renderer failed, using DOM', e); }
    }
    // Keystrokes → server
    term.onData((data) => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(data);
      }
    });
    // Explicit paste handler. xterm's built-in paste path was not firing in
    // this app (Ctrl+V produced nothing while typing worked) — the terminal
    // lives in a fixed-position overlay and the paste event wasn't reaching
    // xterm's helper textarea. Catch it ourselves on the container in the
    // capture phase, before the global window 'paste' listeners (chat image
    // paste), and inject via term.paste() so the text flows through onData →
    // WS → pty and bracketed-paste mode is honoured for TUIs like claude.
    this.containerEl.addEventListener('paste', (e) => {
      const text = e.clipboardData && e.clipboardData.getData('text/plain');
      if (!text) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      try {
        term.paste(text);
      } catch {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(text);
      }
    }, true);
    // Swallow Ctrl+V / Ctrl+Shift+V at the keyboard level. We deliver paste via
    // the explicit 'paste' listener above; if xterm ALSO sent the Ctrl+V control
    // char (\x16) to the pty, claude Code's TUI reads it as "paste image from
    // clipboard" — showing "pasting… / no images found on clipboard" and eating
    // the text. Returning false stops xterm from sending the key but does NOT
    // preventDefault, so the browser's paste event still fires → our handler.
    term.attachCustomKeyEventHandler((e) => {
      if (e.type === 'keydown' && (e.ctrlKey || e.metaKey) && !e.altKey
          && (e.key === 'v' || e.key === 'V')) {
        return false;
      }
      return true;
    });
    // Local resize → server
    term.onResize(({ cols, rows }) => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        try { this.ws.send(JSON.stringify({ type: 'resize', cols, rows })); } catch {}
      }
    });
    this.term = term;

    // Watch container resize → refit.
    this.resizeObserver = new ResizeObserver(() => this.fitNow());
    this.resizeObserver.observe(this.containerEl);
    return term;
  }

  fitNow() {
    if (!this.fitAddon || !this.term) return;
    try {
      this.fitAddon.fit();
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        const { cols, rows } = this.term;
        try { this.ws.send(JSON.stringify({ type: 'resize', cols, rows })); } catch {}
      }
    } catch {}
  }
  // Several fits over the next few hundred ms — flex/grid dimensions and
  // font load races mean one fit isn't enough.
  fitSoon() {
    requestAnimationFrame(() => this.fitNow());
    setTimeout(() => this.fitNow(), 60);
    setTimeout(() => this.fitNow(), 200);
    setTimeout(() => this.fitNow(), 500);
  }

  setSessionInfo(text) {
    if (this.sessionInfoEl) this.sessionInfoEl.textContent = text || '';
  }

  // ── WebSocket lifecycle ──
  attachWebSocket(sessionId) {
    if (this.ws) {
      this.intentionalClose = true;
      try { this.ws.close(); } catch {}
      this.ws = null;
    }
    this.intentionalClose = false;
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${window.location.host}/ws/terminal/${sessionId}`;
    const ws = new WebSocket(url);
    this.ws = ws;
    ws.onopen = () => {
      // Server pty was created with our initial cols/rows. Refit now that
      // the pane's layout has settled — we now know the true terminal size.
      this.fitSoon();
    };
    ws.onmessage = (ev) => {
      if (this.term) this.term.write(typeof ev.data === 'string' ? ev.data : '');
    };
    ws.onclose = () => {
      // Only paint [disconnected] when something unexpected severed the WS.
      // Clean closes (panel hide, new-session, kill, layout switch) stay
      // silent to avoid stale banners on reopen.
      if (!this.intentionalClose && this.term) {
        this.term.write('\r\n\x1b[2m[disconnected]\x1b[0m\r\n');
      }
      this.intentionalClose = false;
    };
    ws.onerror = (e) => {
      console.warn(`[terminal:${this.paneId}] ws error`, e);
    };
  }

  async _spawnSession(cols, rows) {
    const shellKey = (this.shellPickerEl && this.shellPickerEl.value) || localStorage.getItem(SHELL_KEY) || '';
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

  // Resolve a session for this pane (reattach to stored sessionId if it
  // still lives server-side, otherwise spawn fresh) and connect the WS.
  async open() {
    this.ensureTerm();
    this.fitSoon();
    this._syncGitHubIndicator();

    if (availableShells.length === 0) {
      try { await loadShells(); } catch {}
    }

    let sessionId = this._loadStoredSessionId();
    let reattaching = false;
    if (sessionId) {
      const sessions = await listSessions();
      if (sessions.find((s) => s.session_id === sessionId)) {
        reattaching = true;
      } else {
        sessionId = null;
      }
    }
    if (!sessionId) {
      const cols = (this.term && this.term.cols) || 100;
      const rows = (this.term && this.term.rows) || 30;
      try {
        const spawned = await this._spawnSession(cols, rows);
        sessionId = spawned.session_id;
        this._saveStoredSessionId(sessionId);
      } catch (e) {
        if (this.term) this.term.write(`\r\n\x1b[31m[spawn failed: ${e.message}]\x1b[0m\r\n`);
        return;
      }
    }
    this.sessionId = sessionId;
    // On reattach, wipe the local xterm buffer so the server's scrollback
    // replay paints clean (otherwise we'd stack the prior frame, the
    // [disconnected] line, and the replay on top of each other).
    if (reattaching && this.term) {
      try { this.term.reset(); } catch {}
    }
    this.setSessionInfo(`${sessionId.slice(0, 8)} • ${reattaching ? 'reattaching…' : 'attaching…'}`);
    this.attachWebSocket(sessionId);
    // Refresh meta with real pid + shell once we have it.
    listSessions().then((sessions) => {
      const me = sessions.find((s) => s.session_id === sessionId);
      if (me) {
        const shellShort = me.shell.split(/[\\/]/).pop();
        this.setSessionInfo(`${sessionId.slice(0, 8)} • ${shellShort} • pid ${me.pid}`);
      }
    });
    setTimeout(() => { try { this.term && this.term.focus(); } catch {} }, 50);
  }

  async killCurrent() {
    if (!this.sessionId) return;
    await killSession(this.sessionId);
    this._saveStoredSessionId(null);
    this.sessionId = null;
    if (this.ws) { this.intentionalClose = true; try { this.ws.close(); } catch {} this.ws = null; }
    if (this.term) this.term.write('\r\n\x1b[2m[killed]\x1b[0m\r\n');
    this.setSessionInfo('');
  }

  async newSession() {
    // Keep the previous pty alive in the background (the user can
    // `claude --resume` it later if needed). Just switch to a fresh one.
    this._saveStoredSessionId(null);
    this.sessionId = null;
    if (this.ws) { this.intentionalClose = true; try { this.ws.close(); } catch {} this.ws = null; }
    if (this.term) this.term.reset();
    await this.open();
  }

  // ── GitHub per-pty toggle ──
  _ghStorageKey() { return GH_TOGGLE_KEY_PREFIX + (this.sessionId || '_pending'); }
  _ghLoad() {
    try {
      const v = sessionStorage.getItem(this._ghStorageKey());
      return v === null ? true : v === 'true';
    } catch { return true; }
  }
  _ghSave(on) {
    try { sessionStorage.setItem(this._ghStorageKey(), String(!!on)); } catch {}
  }
  _ghApplyVisual(on) {
    if (!this.ghIndicatorEl) return;
    this.ghIndicatorEl.classList.toggle('is-on', !!on);
    this.ghIndicatorEl.classList.toggle('is-off', !on);
    this.ghIndicatorEl.title = on
      ? 'GitHub auth is live in this terminal (click to disable for this session)'
      : 'GitHub auth disabled in this terminal (click to re-enable)';
  }
  _ghSend(on) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    try { this.ws.send(JSON.stringify({ type: 'set_github', on: !!on })); } catch {}
  }
  async _syncGitHubIndicator() {
    if (!this.ghGroupEl || !this.ghIndicatorEl) return;
    try {
      const r = await fetch('/api/github/integration', { credentials: 'same-origin' });
      if (!r.ok) { this.ghGroupEl.style.display = 'none'; return; }
      const info = await r.json();
      // Master toggle off or not configured → hide the group entirely.
      if (!info || !info.configured || info.enabled === false) {
        this.ghGroupEl.style.display = 'none';
        return;
      }
      this.ghGroupEl.style.display = '';
      if (this.ghNameEl) this.ghNameEl.textContent = info.github_username || '';
      const initialOn = this._ghLoad();
      this._ghApplyVisual(initialOn);
      this.ghIndicatorEl.onclick = (e) => {
        e.stopPropagation();
        const wasOn = this.ghIndicatorEl.classList.contains('is-on');
        const nextOn = !wasOn;
        this._ghApplyVisual(nextOn);
        this._ghSave(nextOn);
        this._ghSend(nextOn);
        setTimeout(() => { try { this.term && this.term.focus(); } catch {} }, 30);
      };
      if (this.ghSettingsEl) {
        this.ghSettingsEl.onclick = (e) => {
          e.stopPropagation();
          try { if (window.settingsModule) window.settingsModule.open('github'); } catch {}
        };
      }
    } catch { if (this.ghGroupEl) this.ghGroupEl.style.display = 'none'; }
  }
}

// ── Multi-pane manager — owns the layout + the pane fleet. ──
// We instantiate panes lazily (only the ones the current layout uses get
// mounted) and persist their state across layout switches. Switching
// from 4 → 1 hides B/C/D's DOM but keeps their ptys alive on the server;
// flipping back restores them by reattaching to the saved sessionIds.

const panes = new Map();  // paneId → TerminalPane

function _activePaneIds(layout) {
  return LAYOUT_PANE_IDS[layout] || LAYOUT_PANE_IDS['1'];
}

function _loadLayout() {
  try {
    const v = localStorage.getItem(LAYOUT_KEY);
    if (v && LAYOUT_PANE_IDS[v]) return v;
  } catch {}
  return '1';
}
function _saveLayout(layout) {
  try { localStorage.setItem(LAYOUT_KEY, layout); } catch {}
}

async function _applyLayout(layout) {
  const panel = $('terminal-panel');
  const grid = $('terminal-grid');
  if (!panel || !grid) return;
  if (!LAYOUT_PANE_IDS[layout]) layout = '1';
  panel.dataset.layout = layout;
  _saveLayout(layout);
  const wantedIds = _activePaneIds(layout);

  // Mount + open any pane the layout needs that isn't already up.
  for (const paneId of wantedIds) {
    let pane = panes.get(paneId);
    if (!pane) {
      pane = new TerminalPane(paneId);
      panes.set(paneId, pane);
    }
    if (!pane.mounted) pane.mount(grid);
    pane.show();
    // Reopen if the pane needs a fresh session OR its WS is dead. The
    // dead-WS case happens when closeTerminal hides the panel: it tears
    // down each pane's WebSocket but leaves the pane's sessionId set so
    // we can reattach (not respawn) on reopen. Without the WS check here
    // a reopen would skip pane.open(), the xterm would render but no
    // bytes would flow — the user sees a frozen terminal.
    const wsLive = pane.ws && pane.ws.readyState === WebSocket.OPEN;
    if (!pane.sessionId || !wsLive) {
      // Fire open() but don't await — we want all panes to spawn in parallel
      // so the user doesn't watch them appear one-at-a-time.
      pane.open().catch(e => console.warn(`[terminal:${paneId}] open failed`, e));
    } else {
      pane.fitSoon();
    }
  }
  // Hide panes the new layout doesn't include (but keep their ptys alive).
  for (const [paneId, pane] of panes) {
    if (!wantedIds.includes(paneId)) pane.hide();
  }
  // Focus the first pane in the new layout so keyboard input goes
  // somewhere predictable.
  const first = panes.get(wantedIds[0]);
  if (first) setTimeout(() => first.focus(), 60);
}

async function openTerminal() {
  const panel = $('terminal-panel');
  if (!panel) return;
  const alreadyVisible = !panel.classList.contains('hidden');
  // If panel is already visible AND the active pane has a live WS,
  // treat the re-click as "focus the terminal" rather than "tear down +
  // reattach" (which would bounce ptys for no user-visible reason and
  // occasionally trigger stray BEL on reattach).
  if (alreadyVisible) {
    const wantedIds = _activePaneIds(panel.dataset.layout || '1');
    const first = panes.get(wantedIds[0]);
    if (first && first.ws && first.ws.readyState === WebSocket.OPEN) {
      try { first.focus(); } catch {}
      return;
    }
  }
  panel.classList.remove('hidden');
  // Take the full window — terminal apps (claude TUI, etc.) adapt their
  // layouts to terminal width, and the sidebar eating ~240px makes them
  // render in compact / truncated modes. Hiding the sidebar while the
  // terminal is the active panel gets you a near-Windows-Terminal-width
  // working area; closeTerminal() restores the sidebar.
  document.body.classList.add('terminal-fullscreen');
  // Collapse the sidebar via its real .hidden state (not a blunt display:none)
  // so the hamburger/rail can toggle it back — clicking re-opens it as an
  // overlay over the terminal. Remember whether it was already collapsed so
  // closeTerminal restores the exact prior state.
  {
    const sb = document.getElementById('sidebar');
    if (sb) {
      _sidebarWasHiddenBeforeTerminal = sb.classList.contains('hidden');
      sb.classList.add('hidden');
    }
  }

  // Make sure the shells list is loaded before any pane spawns — otherwise
  // panes silently fall back to default bash even when the user picked pwsh.
  if (availableShells.length === 0) {
    try { await loadShells(); } catch {}
  }

  await _applyLayout(_loadLayout());
  // The class toggle + layout change can shift the panel's available
  // width by 240+px. Refit every visible pane so xterm matches the new
  // size and the pty gets a resize event.
  for (const pane of panes.values()) {
    if (pane.rootEl && pane.rootEl.style.display !== 'none') pane.fitSoon();
  }
}

function closeTerminal() {
  const modal = $('terminal-panel');
  if (modal) modal.classList.add('hidden');
  // Restore the sidebar that openTerminal hid so the rest of Odysseus
  // is usable again. Keep this regardless of why we're closing (Esc,
  // close button, sidebar-nav click) so the sidebar never gets stuck
  // hidden.
  document.body.classList.remove('terminal-fullscreen');
  // Restore the sidebar to exactly the state it was in before the terminal
  // opened (it may have been collapsed by the user already).
  {
    const sb = document.getElementById('sidebar');
    if (sb && !_sidebarWasHiddenBeforeTerminal) sb.classList.remove('hidden');
  }
  // Close every pane's WS but keep their ptys alive — re-opening the
  // panel will reattach. We mark each close as intentional so ws.onclose
  // stays quiet (otherwise the [disconnected] line gets baked into the
  // xterm buffer and surfaces on every reopen).
  for (const pane of panes.values()) {
    if (pane.ws) {
      pane.intentionalClose = true;
      try { pane.ws.close(); } catch {}
      pane.ws = null;
    }
  }
}

// Re-apply the xterm theme on every pane when the user flips Odysseus
// theme classes on <html> or <body>. Cheap MutationObserver — only fires
// on class/data-theme attribute mutations, which only change when the
// user actively switches themes.
function _watchThemeChanges() {
  if (!('MutationObserver' in window)) return;
  const apply = () => {
    const theme = _readTerminalTheme();
    for (const pane of panes.values()) {
      if (!pane.term) continue;
      try { pane.term.options.theme = theme; } catch {}
    }
  };
  const obs = new MutationObserver((muts) => {
    for (const m of muts) {
      if (m.attributeName === 'class' || m.attributeName === 'data-theme') {
        apply();
        return;
      }
    }
  });
  obs.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme'] });
  if (document.body) {
    obs.observe(document.body, { attributes: true, attributeFilter: ['class', 'data-theme'] });
  }
}

function wireUp() {
  const openBtn = $('sidebar-terminal-btn');
  if (openBtn) openBtn.addEventListener('click', openTerminal);
  _watchThemeChanges();
  loadShells();

  const closeBtn = $('close-terminal-panel');
  if (closeBtn) closeBtn.addEventListener('click', closeTerminal);

  // Layout picker — flipping it shows/hides panes (keeps ptys alive).
  const layoutPicker = $('terminal-layout-picker');
  if (layoutPicker) {
    layoutPicker.value = _loadLayout();
    layoutPicker.addEventListener('change', () => {
      _applyLayout(layoutPicker.value);
    });
  }

  // Close on Esc when modal is open.
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const modal = $('terminal-panel');
      if (modal && !modal.classList.contains('hidden')) closeTerminal();
    }
  });

  // Window resize → refit every visible pane.
  window.addEventListener('resize', () => {
    const modal = $('terminal-panel');
    if (!modal || modal.classList.contains('hidden')) return;
    for (const pane of panes.values()) {
      if (pane.rootEl && pane.rootEl.style.display !== 'none') pane.fitNow();
    }
  });

  // Bug fix: terminal panel is a fixed-position overlay, so clicking
  // another sidebar nav item doesn't visually do anything while the
  // terminal is open (the target view renders behind the overlay).
  // Workaround: any click on a sidebar nav item other than the Terminal
  // button itself closes the terminal panel first.
  document.addEventListener('click', (e) => {
    const panel = $('terminal-panel');
    if (!panel || panel.classList.contains('hidden')) return;
    const navItem = e.target.closest(
      '#sidebar-new-chat-btn, #sidebar-search-btn, #sidebar-terminal-btn, ' +
      '#sidebar-brand-btn, #email-section-title, ' +
      '#tools-section .list-item'
    );
    if (!navItem) return;
    if (navItem.id === 'sidebar-terminal-btn') return;  // re-click handled separately
    closeTerminal();
  }, true);  // capture phase so we run before the target's own handler
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', wireUp);
} else {
  wireUp();
}
