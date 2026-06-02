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
const POLL_INTERVAL_MS = 90 * 1000;        // notification-poll cadence

let _writeEnabled = false;  // mirrors the server-side write_enabled flag
let _notifyEnabled = false; // mirrors the server-side notify_enabled flag
let _configured = false;     // mirrors `configured` from /api/github/integration
let _pollTimer = null;       // setInterval handle for notification poller
let _unreadCount = 0;        // most-recent unread-notif count from poller

function $(id) { return document.getElementById(id); }

async function _fetchIntegration() {
  try {
    const r = await fetch('/api/github/integration', { credentials: 'same-origin' });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

function _loadToggle() {
  // Fork default: on. Once the user has GitHub configured, the toggle
  // defaults to ON for every new session — same philosophy as web/bash
  // in app.js's loadToolPref. Explicit off persists via localStorage.
  try {
    const v = localStorage.getItem(TOGGLE_KEY);
    return v === null ? true : v === 'true';
  } catch { return true; }
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
  // Treat a paused integration (enabled=false) the same as not-configured
  // for chat purposes — the button hides, the toggle clears. The PAT is
  // still stored server-side; user just flipped the master switch off in
  // Settings → Integrations. Re-enabling re-shows the button on next refresh.
  const _active = _configured && info && info.enabled !== false;
  _writeEnabled = !!(info && info.write_enabled);
  _notifyEnabled = !!(info && info.notify_enabled);
  _setButtonVisibility(_active);
  // If integration was deleted OR paused while toggle was on, force it off.
  if (!_active) _setEnabled(false);
  // Re-mirror the write_enabled state into the hidden checkbox so chat.js
  // picks it up on the next submit without waiting for a click.
  _setEnabled($('gh-toggle')?.checked || false);
  // Start / stop the notification poller based on opt-in state.
  _syncNotifPoller();
}

// ── Notification poller ──
// Server-side endpoint caches the GitHub /notifications call for 60s, so
// even with multiple Odysseus tabs polling every 90s the API stays well
// under GitHub's rate limit. The poller only runs while configured AND
// notify_enabled — turning either off stops it immediately.

async function _pollUnread() {
  // The notif poller piggybacks on the same active=configured+enabled
  // gate. If the integration is paused, we stop hitting GitHub entirely.
  if (!_configured || !_notifyEnabled) return;
  // (Note: _refresh already toggles button visibility on pause; the poll
  // gate here is a second line so a long-running interval doesn't keep
  // polling between refreshes.)
  try {
    const r = await fetch('/api/github/notifications/count', { credentials: 'same-origin' });
    if (!r.ok) return;
    const data = await r.json();
    _unreadCount = Math.max(0, Number(data.count || 0));
    _renderBadge();
  } catch {}
}

function _syncNotifPoller() {
  if (_configured && _notifyEnabled) {
    if (!_pollTimer) {
      _pollTimer = setInterval(_pollUnread, POLL_INTERVAL_MS);
      // Fire one immediately so the badge is accurate on toggle-on.
      _pollUnread();
    }
  } else {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
    _unreadCount = 0;
    _renderBadge();
  }
}

function _renderBadge() {
  const btn = $('gh-toggle-btn');
  if (!btn) return;
  let badge = btn.querySelector('.gh-toggle-badge');
  if (_unreadCount > 0) {
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'gh-toggle-badge';
      btn.appendChild(badge);
    }
    badge.textContent = _unreadCount > 99 ? '99+' : String(_unreadCount);
    btn.title = `GitHub access (${_unreadCount} unread notification${_unreadCount === 1 ? '' : 's'})`;
  } else if (badge) {
    badge.remove();
    btn.title = 'GitHub access (PR-aware chat)';
  }
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
