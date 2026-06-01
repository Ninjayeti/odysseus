/* GitHub integration — chat-input toggle.
 *
 * Single-click toggle (same pattern as web-toggle-btn / bash-toggle-btn).
 * Hidden until the user has a PAT configured in Settings -> Integrations;
 * shows automatically once the integration is set up.
 *
 * Write actions are configured in Settings (a separate `write_enabled`
 * flag) — not per-conversation here. This button only controls READ access
 * for the current chat. When ON, chat.js sends `allow_github=true` AND
 * `allow_github_write=true` (the latter sourced from the server-side
 * integration row), so the chat-input toggle is a single "do GitHub now"
 * switch and Settings is the one place to grant write privilege.
 */

const TOGGLE_KEY = 'odysseus-gh-toggle';  // persisted toggle state across reloads

let _writeEnabled = false;  // mirrors the server-side write_enabled flag
let _configured = false;     // mirrors `configured` from /api/github/integration

function $(id) { return document.getElementById(id); }

async function _fetchIntegration() {
  try {
    const r = await fetch('/api/github/integration', { credentials: 'same-origin' });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

function _loadToggle() {
  try { return localStorage.getItem(TOGGLE_KEY) === 'true'; } catch { return false; }
}
function _saveToggle(val) {
  try { localStorage.setItem(TOGGLE_KEY, String(!!val)); } catch {}
}

/** Sync hidden checkbox + button state. */
function _setEnabled(on) {
  const chk = $('gh-toggle');
  const writeChk = $('gh-toggle-write');
  const btn = $('gh-toggle-btn');
  if (chk) chk.checked = !!on;
  // The write flag rides with the read toggle — server's write_enabled gates
  // it, but when GitHub is OFF for the chat there's nothing to write anyway,
  // so we set the form-field checkbox only when both apply.
  if (writeChk) writeChk.checked = !!on && _writeEnabled;
  if (btn) btn.classList.toggle('active', !!on);
  _saveToggle(on);
}

function _setButtonVisibility(visible) {
  const btn = $('gh-toggle-btn');
  if (btn) btn.style.display = visible ? '' : 'none';
}

async function _refresh() {
  const info = await _fetchIntegration();
  _configured = !!(info && info.configured);
  _writeEnabled = !!(info && info.write_enabled);
  _setButtonVisibility(_configured);
  // If integration was deleted while toggle was on, force it off.
  if (!_configured) _setEnabled(false);
  // Re-mirror the write_enabled state into the hidden checkbox so chat.js
  // picks it up on the next submit without waiting for a click.
  _setEnabled($('gh-toggle')?.checked || false);
}

function _wireUp() {
  const btn = $('gh-toggle-btn');
  if (!btn) return;  // markup not present (e.g. compare mode strips toolbar)

  // Restore previous session's toggle state.
  _setEnabled(_loadToggle());

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const chk = $('gh-toggle');
    _setEnabled(!(chk && chk.checked));
  });

  // Initial fetch — drives button visibility + write_enabled state.
  _refresh();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _wireUp);
} else {
  _wireUp();
}

// Settings page calls this after PAT save/delete or write_enabled toggle so
// the chat-input button reflects the new state without a page reload.
window.githubToggle = {
  refresh: _refresh,
};
