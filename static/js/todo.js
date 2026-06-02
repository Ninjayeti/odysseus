import uiModule from './ui.js';

const API = window.location.origin;
const LABEL = 'hive-todo';
let _noteId = null;
let _items = [];
let _mounted = false;

async function _ensureNote() {
  if (_noteId) return _noteId;
  const res = await fetch(`${API}/api/notes`);
  if (!res.ok) return null;
  const notes = await res.json();
  const existing = notes.find(n => n.label === LABEL && !n.archived);
  if (existing) {
    _noteId = existing.id;
    _items = _parseItems(existing.items);
    return _noteId;
  }
  const fd = new FormData();
  fd.append('title', 'To-Do');
  fd.append('note_type', 'checklist');
  fd.append('label', LABEL);
  fd.append('pinned', 'true');
  fd.append('items', JSON.stringify([]));
  const cr = await fetch(`${API}/api/notes`, { method: 'POST', body: fd });
  if (!cr.ok) return null;
  const created = await cr.json();
  _noteId = created.id;
  _items = [];
  return _noteId;
}

function _parseItems(raw) {
  if (!raw) return [];
  try {
    const arr = typeof raw === 'string' ? JSON.parse(raw) : raw;
    return Array.isArray(arr) ? arr : [];
  } catch { return []; }
}

async function _save() {
  if (!_noteId) return;
  const fd = new FormData();
  fd.append('items', JSON.stringify(_items));
  await fetch(`${API}/api/notes/${_noteId}`, { method: 'PATCH', body: fd });
}

async function _load() {
  await _ensureNote();
  if (!_noteId) return;
  const res = await fetch(`${API}/api/notes/${_noteId}`);
  if (!res.ok) return;
  const note = await res.json();
  _items = _parseItems(note.items);
}

function _render() {
  const list = document.getElementById('todo-list');
  const count = document.getElementById('todo-count');
  const empty = document.getElementById('todo-empty');
  if (!list) return;

  const pending = _items.filter(i => !i.done).length;
  if (count) {
    count.textContent = pending > 0 ? pending : '';
    count.style.display = pending > 0 ? '' : 'none';
  }

  list.innerHTML = '';
  if (_items.length === 0) {
    if (empty) empty.style.display = '';
    return;
  }
  if (empty) empty.style.display = 'none';

  const undone = _items.filter(i => !i.done);
  const done = _items.filter(i => i.done);

  [...undone, ...done].forEach((item, _i) => {
    const idx = _items.indexOf(item);
    const row = document.createElement('div');
    row.className = 'todo-item' + (item.done ? ' done' : '');

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!item.done;
    cb.className = 'todo-cb';
    cb.addEventListener('change', () => {
      _items[idx].done = cb.checked;
      _save();
      _render();
    });

    const text = document.createElement('span');
    text.className = 'todo-text';
    text.textContent = item.text || '';

    const del = document.createElement('button');
    del.className = 'todo-del';
    del.innerHTML = '&times;';
    del.title = 'Remove';
    del.addEventListener('click', (e) => {
      e.stopPropagation();
      _items.splice(idx, 1);
      _save();
      _render();
    });

    row.appendChild(cb);
    row.appendChild(text);
    row.appendChild(del);
    list.appendChild(row);
  });
}

function _addItem(text) {
  if (!text.trim()) return;
  _items.push({ text: text.trim(), done: false });
  _save();
  _render();
}

function mount() {
  if (_mounted) return;
  _mounted = true;

  const section = document.getElementById('todo-section');
  const header = section ? section.querySelector('.section-header-flex') : null;
  if (header) {
    header.addEventListener('click', () => {
      section.classList.toggle('collapsed');
      try { localStorage.setItem('odysseus-todo-collapsed', section.classList.contains('collapsed') ? '1' : ''); } catch {}
    });
    if (localStorage.getItem('odysseus-todo-collapsed') === '1') {
      section.classList.add('collapsed');
    }
  }

  const input = document.getElementById('todo-input');
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        _addItem(input.value);
        input.value = '';
      }
    });
  }

  const addBtn = document.getElementById('todo-add-btn');
  if (addBtn) {
    addBtn.addEventListener('click', () => {
      if (input) {
        _addItem(input.value);
        input.value = '';
        input.focus();
      }
    });
  }

  _load().then(_render);
}

function refresh() {
  _load().then(_render);
}

export default { mount, refresh };
