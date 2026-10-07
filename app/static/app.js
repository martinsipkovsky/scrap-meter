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

// ---- left menu: hide / show (remembered per browser; on narrow screens it opens over the page)
(function () {
  const btn = document.getElementById('navToggle');
  if (!btn) return;
  const root = document.documentElement;
  const narrow = () => window.matchMedia('(max-width: 760px)').matches;
  const sync = () => {
    const open = narrow() ? root.classList.contains('nav-open') : !root.classList.contains('nav-collapsed');
    btn.setAttribute('aria-expanded', String(open));
    btn.title = open ? 'Hide the menu' : 'Show the menu';
    btn.setAttribute('aria-label', btn.title);
    window.dispatchEvent(new Event('navchange'));
  };
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (narrow()) { root.classList.toggle('nav-open'); }
    else {
      root.classList.toggle('nav-collapsed');
      try { localStorage.setItem('sm.nav', root.classList.contains('nav-collapsed') ? 'collapsed' : 'open'); } catch (err) {}
    }
    sync();
  });
  // a tap next to the open menu only closes it (it doesn't also open what is under it)
  document.addEventListener('click', (e) => {
    if (root.classList.contains('nav-open') && !e.target.closest('.sidebar')) {
      e.preventDefault(); e.stopPropagation();
      root.classList.remove('nav-open'); sync();
    }
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && root.classList.contains('nav-open')) { root.classList.remove('nav-open'); sync(); }
  });
  window.matchMedia('(max-width: 760px)').addEventListener('change', () => { root.classList.remove('nav-open'); sync(); });
  sync();
})();

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

// ---- piece rules (pictures -> pieces per job, app.pieces) ---------------------
function pieceRuleText(r) {
  if (!r) return '1 picture = 1 piece';
  const n = r.pictures, parts = [];
  parts.push(r.group_key ? `pictures with the same '${r.group_key}' are one piece (up to ${n})` : `${n} pictures = 1 piece`);
  parts.push(r.verdict === 'min_ok' && r.min_ok < n ? `OK with at least ${r.min_ok} OK pictures` : 'OK only if all pictures are OK');
  if (r.nok_closes) parts.push('a NOK ends the piece');
  if (r.timeout_s) parts.push(`judged after ${r.timeout_s} s`);
  parts.push({ nok: 'missing pictures make it NOK', judge: 'missing pictures are left out', discard: 'incomplete pieces are not counted' }[r.missing]);
  return parts.join(', ');
}
function openPieceText(p) {
  const n = (p.ok || 0) + (p.nok || 0);
  return `${p.station}: ${n} picture${n === 1 ? '' : 's'} so far (${p.ok} OK, ${p.nok} NOK)` + (p.id != null ? `, piece ${p.id}` : '')
    + (p.since ? ', since ' + fmtAgo(new Date(p.since * 1000).toISOString()) : '');
}
function _pieceModal() {
  let m = document.getElementById('pieceModal');
  if (m) return m;
  const field = (label, input, hint) => el('div', {}, el('label', {}, label), input, hint ? el('div', { class: 'hint' }, hint) : null);
  m = el('div', { class: 'modal-back', id: 'pieceModal' }, el('div', { class: 'modal', style: 'max-width:620px' },
    el('h2', { id: 'pr_title' }, 'Pieces'),
    el('div', { id: 'pr_error', class: 'error', style: 'display:none' }),
    el('p', { class: 'muted', style: 'margin-top:0' }, 'When the camera takes several pictures of one piece, the app can count real pieces: the dashboard, statistics, OEE, notifications and reports then count pieces. The Data log keeps the camera\'s own picture counters.'),
    el('div', { class: 'row' },
      field('Pictures per piece', el('input', { id: 'pr_n', type: 'number', min: '1', max: '64', value: '1', oninput: () => _pieceExample() }), '1 = every picture is a piece (no rule).'),
      field('A piece is OK when', el('select', { id: 'pr_verdict', onchange: () => _pieceExample() },
        el('option', { value: 'all_ok' }, 'all its pictures are OK'), el('option', { value: 'min_ok' }, 'at least K pictures are OK')))),
    el('div', { class: 'row', id: 'pr_kRow' },
      field('K: OK pictures needed', el('input', { id: 'pr_k', type: 'number', min: '1', value: '1', oninput: () => _pieceExample() }), 'e.g. 2 of 3: one NOK picture is tolerated.')),
    el('label', { style: 'display:flex;gap:8px;align-items:center;font-weight:normal;margin-top:10px' },
      el('input', { type: 'checkbox', id: 'pr_close', style: 'width:auto;margin:0', onchange: () => _pieceExample() }),
      'A NOK picture ends the piece at once (next cavity when NOK)'),
    el('div', { class: 'hint' }, 'Use it when the machine moves on to the next piece after a NOK picture, so the piece never gets its other pictures.'),
    el('div', { class: 'row' },
      field('Timer (seconds)', el('input', { id: 'pr_timeout', type: 'number', min: '0', step: 'any', placeholder: 'no timer', oninput: () => _pieceExample() }),
        'A piece that hasn\'t got all its pictures this long after its first one is judged anyway.'),
      field('When pictures are missing', el('select', { id: 'pr_missing', onchange: () => _pieceExample() },
        el('option', { value: 'nok' }, 'the piece is NOK'), el('option', { value: 'judge' }, 'judge only the pictures taken'),
        el('option', { value: 'discard' }, 'don\'t count the piece')),
        'Applies at the timer, a job change, a production stop and a new piece id.')),
    field('Piece id value (optional)', el('input', { id: 'pr_key', placeholder: 'e.g. piece_id or cavity', oninput: () => _pieceExample() }),
      'A value the device sends with each picture. Pictures with the same id are one piece; a new id ends the open piece. Leave empty to group by count.'),
    el('div', { id: 'pr_example', class: 'warnbox', style: 'margin-top:12px' }),
    el('div', { class: 'hint' }, 'Counted from now on; past data isn\'t changed. With one picture per read (listeners, events) the grouping is exact. When a polled counter rose by several pictures between two reads, the order is unknown and every NOK picture is taken to spoil a piece of its own: those counts are an estimate.'),
    el('div', { class: 'actions', style: 'margin-top:16px;justify-content:space-between' },
      el('button', { class: 'btn danger small', id: 'pr_remove', onclick: () => _savePieceRule(null) }, 'No rule'),
      el('div', { class: 'actions' },
        el('button', { class: 'btn secondary', onclick: () => closeModal('pieceModal') }, 'Cancel'),
        el('button', { class: 'btn', onclick: () => _savePieceRule(_pieceForm()) }, 'Save')))));
  document.body.append(m);
  return m;
}
let _pieceJob = null, _pieceDone = null;
function _pieceForm() {
  const v = (id) => document.getElementById(id).value.trim();
  const n = parseInt(v('pr_n'), 10) || 1;
  return { pictures: n, verdict: v('pr_verdict'), min_ok: Math.min(parseInt(v('pr_k'), 10) || n, n),
    nok_closes: document.getElementById('pr_close').checked, timeout_s: v('pr_timeout') === '' ? null : parseFloat(v('pr_timeout')),
    missing: v('pr_missing'), group_key: v('pr_key') || null };
}
function _pieceExample() {
  const r = _pieceForm();
  document.getElementById('pr_kRow').style.display = r.verdict === 'min_ok' ? '' : 'none';
  const box = document.getElementById('pr_example');
  if (r.pictures <= 1 && !r.group_key) { box.textContent = 'No rule: every OK / NOK picture counts as one piece.'; return; }
  const n = r.pictures, k = r.verdict === 'min_ok' ? r.min_ok : n, allowed = n - k;
  const pics = (nok, ok) => [...Array(nok).fill('NOK'), ...Array(ok).fill('OK')].join(', ');
  const ex = [pics(0, n) + ' → OK +1'];
  if (allowed > 0) ex.push(pics(allowed, n - allowed) + ' → OK +1');
  if (r.nok_closes && allowed + 1 < n) ex.push(pics(allowed + 1, 0) + ' → NOK +1 at once; the next picture starts a new piece');
  else ex.push(pics(allowed + 1, n - allowed - 1) + ' → NOK +1');
  const miss = { nok: 'NOK +1', judge: allowed ? 'judged on the pictures taken' : 'OK +1 if they were all OK, else NOK +1', discard: 'not counted' }[r.missing];
  if (n > 1) ex.push(`only ${n - 1} of ${n} pictures came (${pics(0, n - 1)})` + (r.timeout_s ? ` within ${r.timeout_s} s` : ' before a job change or stop') + ` → ${miss}`);
  box.innerHTML = '';
  box.append(el('strong', {}, pieceRuleText(r)), ...ex.map(t => el('div', {}, t)));
}
function openPieceRule(job, onDone) {
  _pieceModal();
  _pieceJob = job; _pieceDone = onDone;
  const r = job.piece_rule || {};
  document.getElementById('pr_title').textContent = 'Pieces of job ' + job.name;
  document.getElementById('pr_error').style.display = 'none';
  document.getElementById('pr_n').value = r.pictures || 1;
  document.getElementById('pr_verdict').value = r.verdict || 'all_ok';
  document.getElementById('pr_k').value = r.min_ok || r.pictures || 1;
  document.getElementById('pr_close').checked = !!r.nok_closes;
  document.getElementById('pr_timeout').value = r.timeout_s || '';
  document.getElementById('pr_missing').value = r.missing || 'nok';
  document.getElementById('pr_key').value = r.group_key || '';
  document.getElementById('pr_remove').style.display = job.piece_rule ? '' : 'none';
  _pieceExample();
  openModal('pieceModal');
}
async function _savePieceRule(rule) {
  const err = document.getElementById('pr_error');
  try {
    const r = await patchJSON('/api/jobs/' + _pieceJob.id, { piece_rule: rule });
    closeModal('pieceModal');
    toast(_pieceJob.name + ': ' + r.piece_rule_text);
    if (_pieceDone) _pieceDone();
  } catch (e) { err.textContent = e.message; err.style.display = 'block'; }
}
// the "Pieces" cell of a jobs table: the rule, the open pieces, and the button
function pieceRuleCell(j, canManage, onDone) {
  return el('div', {},
    el('span', { class: j.piece_rule ? '' : 'muted' }, j.piece_rule_text || pieceRuleText(j.piece_rule)),
    ...(j.open_pieces || []).map(p => el('div', { class: 'hint' }, 'Open: ' + openPieceText(p))),
    canManage ? el('div', {}, el('button', { class: 'btn secondary small', style: 'margin-top:4px', onclick: () => openPieceRule(j, onDone) }, j.piece_rule ? 'Edit rule' : 'Set rule')) : null);
}
