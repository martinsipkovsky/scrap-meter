// Small fetch helpers shared by every page.
async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  if (resp.status === 204) return null;
  let data = null;
  try { data = await resp.json(); } catch (e) { /* no body */ }
  if (!resp.ok) {
    const msg = (data && (data.detail || data.message)) || resp.statusText;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}
const getJSON = (u) => api('GET', u);
const postJSON = (u, b) => api('POST', u, b);
const patchJSON = (u, b) => api('PATCH', u, b);
const delJSON = (u) => api('DELETE', u);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children) {
    if (c === null || c === undefined) continue;
    node.append(c.nodeType ? c : document.createTextNode(c));
  }
  return node;
}

function fmtPct(x) { return (x * 100).toFixed(1) + '%'; }
function scrapClass(rate) { return rate >= 0.05 ? 'fail' : (rate >= 0.02 ? 'warn' : 'pass'); }

function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }

function parseJSONField(text, fallback) {
  const t = (text || '').trim();
  if (!t) return fallback;
  try { return JSON.parse(t); } catch (e) { throw new Error('Invalid JSON in config: ' + e.message); }
}

function toast(msg, isErr) {
  const t = el('div', { class: 'error', style:
    'position:fixed;bottom:20px;right:20px;z-index:100;max-width:360px;' +
    (isErr ? '' : 'background:rgba(34,197,94,.12);border-color:rgba(34,197,94,.4);color:#bbf7d0;') }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), 4000);
}

// Production state badges shared by the dashboard and the device view.
const PROD_BADGE = {
  running: ['ok', 'In production'],
  idle: ['off', 'Not in production'],
  stopped: ['warn', 'Stopped'],
};
function fmtAgo(iso) {
  if (!iso) return 'never';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return Math.round(s) + ' s ago';
  if (s < 5400) return Math.round(s / 60) + ' min ago';
  if (s < 172800) return Math.round(s / 3600) + ' h ago';
  return Math.round(s / 86400) + ' days ago';
}
function fmtDateTime(iso) {
  const d = new Date(iso);
  const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return d.toDateString() === new Date().toDateString() ? t : d.toLocaleDateString([], { day: '2-digit', month: '2-digit' }) + ' ' + t;
}
function idleText(d) {
  return 'No OK increase for ' + d.idle_timeout_min + ' min (last ' + fmtAgo(d.last_pass_change_at) + ')';
}
// the devices of a station's sources in production now, else the one active last
function activeLine(d) {
  if (d.active_devices && d.active_devices.length) {
    return el('div', { class: 'active-line on' }, '▶ Active: ' + d.active_devices.join(', '));
  }
  if (d.last_active) {
    return el('div', { class: 'active-line off', title: new Date(d.last_active.at).toLocaleString() },
      'Last active: ' + d.last_active.device + ' · ' + fmtDateTime(d.last_active.at));
  }
  return null;
}
function downloadJSON(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const a = el('a', { href: URL.createObjectURL(blob), download: filename });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// Station comments (station view and dashboard). See app/comments.py.
function commentSnapshot(c) {
  return (c.job ? 'job ' + c.job + ' · ' : '') + 'OK ' + c.ok + ' / NOK ' + c.nok + ' · scrap ' + fmtPct(c.scrap_rate);
}
function commentItem(c, onDelete) {
  return el('div', { class: 'comment' },
    el('div', { class: 'comment-head' },
      el('strong', {}, c.author || '—'),
      el('span', { title: new Date(c.created_at).toLocaleString() }, fmtDateTime(c.created_at)),
      el('span', {}, '· ' + commentSnapshot(c)),
      onDelete ? el('button', { class: 'btn secondary small', style: 'margin-left:auto', onclick: () => onDelete(c) }, 'Delete') : null),
    el('div', { class: 'comment-text' }, c.text));
}
// A box to write a comment plus the station's comments, newest first.
// opts.isAdmin shows Delete buttons; opts.onChange runs after an add or delete.
function commentsPanel(stationId, opts = {}) {
  const list = el('div', { class: 'comment-list' });
  const ta = el('textarea', { rows: 2, maxlength: 2000, placeholder: 'Write a comment, e.g. what happened or what was changed. Ctrl+Enter saves.' });
  const add = el('button', { class: 'btn' }, 'Add comment');
  async function load() {
    let rows;
    try { rows = await getJSON('/api/stations/' + stationId + '/comments?limit=100'); }
    catch (e) { list.innerHTML = ''; list.append(el('div', { class: 'muted' }, e.message)); return; }
    list.innerHTML = '';
    if (!rows.length) list.append(el('div', { class: 'muted' }, 'No comments yet.'));
    rows.forEach(c => list.append(commentItem(c, opts.isAdmin ? remove : null)));
  }
  async function save() {
    const text = ta.value.trim();
    if (!text) { toast('Write a comment first', true); return; }
    add.disabled = true;
    try {
      await postJSON('/api/stations/' + stationId + '/comments', { text });
      ta.value = ''; toast('Comment saved');
      await load(); if (opts.onChange) opts.onChange();
    } catch (e) { toast(e.message, true); }
    add.disabled = false;
  }
  async function remove(c) {
    if (!confirm('Delete this comment? It is removed from reports too.')) return;
    try { await delJSON('/api/comments/' + c.id); await load(); if (opts.onChange) opts.onChange(); }
    catch (e) { toast(e.message, true); }
  }
  add.addEventListener('click', save);
  ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save(); } });
  load();
  return el('div', {}, el('div', { class: 'comment-form' }, ta, add), list);
}
