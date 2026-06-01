/* GitHub integration — chat-input toggle + popover.
 *
 * Click the GitHub icon in the chat input → popover opens. Popover has:
 *   - "Enable for this chat" switch (mirrors #gh-toggle hidden checkbox)
 *   - "Allow write actions" switch (mirrors #gh-toggle-write, greyed when above is off)
 *   - "Configure in Settings" link → opens Settings → Integrations
 * If no integration is configured, popover shows an empty state with a
 * "Set up GitHub" CTA that opens Settings directly.
 *
 * The hidden checkboxes are what chat.js submits to /api/chat_stream; the
 * popover is just the UI for editing them.
 */

const TOGGLE_KEY = 'odysseus-gh-toggle';        // per-browser persistence of read-toggle state
const WRITE_KEY = 'odysseus-gh-toggle-write';   // ditto for write-toggle

let _integration = null;  // cached integration metadata, refreshed on popover open

function $(id) { return document.getElementById(id); }

async function _fetchIntegration() {
  try {
    const r = await fetch('/api/github/integration', { credentials: 'same-origin' });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

function _loadToggle(key, dflt = false) {
  try {
    const v = localStorage.getItem(key);
    return v === null ? dflt : v === 'true';
  } catch { return dflt; }
}
function _saveToggle(key, val) {
  try { localStorage.setItem(key, String(!!val)); } catch {}
}

/** Sync the hidden #gh-toggle checkbox + the button's active class to a boolean. */
function _setReadEnabled(on) {
  const chk = $('gh-toggle');
  const btn = $('gh-toggle-btn');
  if (chk) chk.checked = !!on;
  if (btn) btn.classList.toggle('active', !!on);
  _saveToggle(TOGGLE_KEY, on);
}

function _setWriteEnabled(on) {
  const chk = $('gh-toggle-write');
  if (chk) chk.checked = !!on;
  _saveToggle(WRITE_KEY, on);
}

/** Reflect the current state into the popover switches. */
function _syncPopoverFromState() {
  const enableSwitch = $('gh-popover-enable');
  const writeSwitch = $('gh-popover-write');
  const readChk = $('gh-toggle');
  const writeChk = $('gh-toggle-write');
  if (enableSwitch && readChk) enableSwitch.checked = readChk.checked;
  if (writeSwitch && writeChk) writeSwitch.checked = writeChk.checked;
  // Write switch is greyed out when read is off — write without read is
  // meaningless (the agent can't act on something it can't see).
  const writeRow = $('gh-popover-row-write');
  if (writeRow) {
    const dim = !(readChk && readChk.checked);
    writeRow.classList.toggle('disabled', dim);
    if (writeSwitch) writeSwitch.disabled = dim;
  }
}

/** Show or hide the entire chat-input button based on whether the user has
 * configured the integration. Pre-config the button isn't useful — there's
 * nothing it can toggle on — so hiding it avoids cluttering the toolbar
 * for users who haven't set up GitHub. Surfaces once a PAT lands. */
function _setButtonVisibility(visible) {
  const wrap = $('gh-toggle-wrap');
  if (wrap) wrap.style.display = visible ? '' : 'none';
}

/** Show the configured / empty state appropriately. */
function _renderPopover(integration) {
  const status = $('gh-popover-status');
  const body = $('gh-popover-body');
  const empty = $('gh-popover-empty');
  if (!body || !empty || !status) return;
  if (integration && integration.configured) {
    _setButtonVisibility(true);
    body.classList.remove('hidden');
    empty.classList.add('hidden');
    status.textContent = `@${integration.github_username || '?'}`;
    status.classList.remove('gh-status-empty');
  } else {
    // No PAT yet — hide the toolbar button entirely. Settings is the place
    // to set it up, not a stray popover in the chat input.
    _setButtonVisibility(false);
    body.classList.add('hidden');
    empty.classList.remove('hidden');
    status.textContent = 'Not configured';
    status.classList.add('gh-status-empty');
    // Force the toggles off — can't use what isn't there.
    _setReadEnabled(false);
    _setWriteEnabled(false);
  }
  _syncPopoverFromState();
}

function _openSettingsToIntegrations() {
  // The settings module exposes `open(tab)` on `window.settingsModule`.
  // Fall back to a click on the existing settings entry if it isn't there.
  try {
    if (window.settingsModule && typeof window.settingsModule.open === 'function') {
      window.settingsModule.open('integrations');
      return;
    }
  } catch {}
  const settingsBtn = document.getElementById('user-bar-settings');
  if (settingsBtn) settingsBtn.click();
}

function _closePopover() {
  const pop = $('gh-popover');
  if (pop) pop.classList.add('hidden');
}

async function _openPopover() {
  const pop = $('gh-popover');
  if (!pop) return;
  pop.classList.remove('hidden');
  // Always refresh on open — the user may have just configured GitHub in
  // Settings and we want the new state to appear without a page reload.
  _integration = await _fetchIntegration();
  _renderPopover(_integration);
}

async function _wireUp() {
  const btn = $('gh-toggle-btn');
  const pop = $('gh-popover');
  if (!btn || !pop) return;  // markup not present (e.g. compare mode strips toolbar)

  // Restore previous session's toggle state from localStorage. Don't enable
  // anything if no integration is configured — that's checked on popover
  // open and would re-disable.
  _setReadEnabled(_loadToggle(TOGGLE_KEY, false));
  _setWriteEnabled(_loadToggle(WRITE_KEY, false));

  // Initial fetch — drives the button's visibility. Configured = visible,
  // not-configured = hidden until the user sets up a PAT in Settings.
  // The settings card calls window.githubToggle.refresh() after save/delete
  // so this also re-runs on those events without a page reload.
  _integration = await _fetchIntegration();
  _renderPopover(_integration);

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (pop.classList.contains('hidden')) _openPopover();
    else _closePopover();
  });

  // Click outside → close.
  document.addEventListener('click', (e) => {
    if (pop.classList.contains('hidden')) return;
    if (pop.contains(e.target) || btn.contains(e.target)) return;
    _closePopover();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !pop.classList.contains('hidden')) _closePopover();
  });

  // Switches — write to hidden checkboxes + persist.
  const enableSw = $('gh-popover-enable');
  const writeSw = $('gh-popover-write');
  if (enableSw) {
    enableSw.addEventListener('change', () => {
      _setReadEnabled(enableSw.checked);
      // Turning off read also disables write (state-wise; the UI greys it).
      if (!enableSw.checked) _setWriteEnabled(false);
      _syncPopoverFromState();
    });
  }
  if (writeSw) {
    writeSw.addEventListener('change', () => {
      _setWriteEnabled(writeSw.checked);
      _syncPopoverFromState();
    });
  }

  // Links into Settings.
  const settingsLink = $('gh-popover-settings-link');
  if (settingsLink) {
    settingsLink.addEventListener('click', (e) => {
      e.stopPropagation();
      _closePopover();
      _openSettingsToIntegrations();
    });
  }
  const setupCta = $('gh-popover-setup-cta');
  if (setupCta) {
    setupCta.addEventListener('click', (e) => {
      e.stopPropagation();
      _closePopover();
      _openSettingsToIntegrations();
    });
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _wireUp);
} else {
  _wireUp();
}

// Expose a tiny API so settings.js can poke us after a save / delete without
// a page reload — e.g. "the user just removed their PAT, please rerender."
window.githubToggle = {
  refresh: async () => {
    _integration = await _fetchIntegration();
    _renderPopover(_integration);
  },
};
