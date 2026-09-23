'use strict';

const $ = (id) => document.getElementById(id);
const prefs = {
  get(key, fallback) { try { return localStorage.getItem('smsgw:' + key) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem('smsgw:' + key, value); } catch { /* private mode */ } },
};

const ui = {
  view: 'messages',
  group: prefs.get('group', 'threads'),
  filter: prefs.get('filter', 'all'),
  sort: prefs.get('sort', 'new'),
  q: '',
  selected: null,
  highlight: null,
  keepUnread: new Set(),
};
let data = null;
let csrf = '';
let busy = false;
let lastSignature = '';
let fwdDirty = false;
let limitDirty = false;

// --- GSM7 --------------------------------------------------------------------

const GSM = '@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà';
const GSM_EXT = '\f^{}\\[~]|€';
const TRANSLIT = Object.fromEntries([...'абвгдеёжзийклмнопрстуфхцчшщъыьэюя'].map((c, i) => [c,
  ['a', 'b', 'v', 'g', 'd', 'e', 'yo', 'zh', 'z', 'i', 'y', 'k', 'l', 'm', 'n', 'o', 'p', 'r', 's', 't', 'u', 'f',
    'kh', 'ts', 'ch', 'sh', 'shch', '', 'y', '', 'e', 'yu', 'ya'][i]]));
const PUNCT = { '«': '"', '»': '"', '“': '"', '”': '"', '„': '"', '‘': "'", '’': "'", '—': '-', '–': '-', '…': '...', '№': 'No', '\u00a0': ' ' };

function gsmInfo(text) {
  let units = 0;
  let invalid = 0;
  for (const ch of text) {
    if (GSM_EXT.includes(ch)) units += 2;
    else if (GSM.includes(ch)) units += 1;
    else { units += 1; invalid += 1; }
  }
  return { units, invalid };
}

function translit(text) {
  return [...text].map((ch) => {
    const low = ch.toLowerCase();
    if (low in TRANSLIT) {
      const v = TRANSLIT[low];
      return ch !== low && v ? v[0].toUpperCase() + v.slice(1) : v;
    }
    return PUNCT[ch] ?? ch;
  }).join('');
}

function bindCounter(textarea, counter) {
  const update = () => {
    const { units, invalid } = gsmInfo(textarea.value);
    counter.textContent = invalid ? `${units} / 160 · ${invalid} симв. не GSM7 — нажмите «В транслит»` : `${units} / 160 · GSM7`;
    counter.classList.toggle('over', units > 160 || invalid > 0);
  };
  textarea.addEventListener('input', update);
  update();
  return update;
}

// --- helpers -----------------------------------------------------------------

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const icon = (name) => h('span', { class: 'i i-' + name, 'aria-hidden': 'true' });

const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
const startOfDay = (ms) => { const d = new Date(ms); d.setHours(0, 0, 0, 0); return d.getTime(); };
const hhmm = (ms) => new Date(ms).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });

function shortTime(ms) {
  if (!ms) return '';
  const today = startOfDay(Date.now());
  const day = startOfDay(ms);
  if (day === today) return hhmm(ms);
  if (today - day === 86400000) return 'вчера';
  const d = new Date(ms);
  if (d.getFullYear() === new Date().getFullYear()) return `${d.getDate()} ${MONTHS[d.getMonth()]}`;
  return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit' });
}

function dayLabel(ms) {
  const today = startOfDay(Date.now());
  const day = startOfDay(ms);
  if (day === today) return 'Сегодня';
  if (today - day === 86400000) return 'Вчера';
  return new Date(ms).toLocaleDateString('ru-RU', { weekday: 'short', day: 'numeric', month: 'long', year: new Date(ms).getFullYear() === new Date().getFullYear() ? undefined : 'numeric' });
}

const dateOnly = (ms) => new Date(ms).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' });
const fullTime = (ms) => (ms ? new Date(ms).toLocaleString('ru-RU') : '—');
const isPhone = (addr) => /^\+?\d{3,15}$/.test(String(addr || '').replace(/[\s()-]/g, ''));
const threadKey = (addr) => (isPhone(addr) ? 'n:' + String(addr).replace(/\D/g, '').slice(-10) : 's:' + String(addr || '?').toLowerCase());

const PALETTE = ['#2f8f7b', '#c2683f', '#7a67ad', '#b4587b', '#55873b', '#a88226', '#3d7ea3', '#8a6e5b'];
function avatar(name, key) {
  let hash = 0;
  for (const ch of key) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  const label = isPhone(name) ? String(name).replace(/\D/g, '').slice(-2) : String(name).replace(/[^\p{L}\p{N}]/gu, '').slice(0, 2).toUpperCase();
  const el = h('div', { class: 'avatar', 'aria-hidden': 'true', text: label || '?' });
  el.style.background = PALETTE[hash % PALETTE.length];
  return el;
}

function toast(text, error = false) {
  const el = h('div', { class: 'toast' + (error ? ' err' : ''), text });
  $('toasts').append(el);
  setTimeout(() => el.remove(), error ? 7000 : 3500);
}

const operationId = () => (crypto.randomUUID ? crypto.randomUUID()
  : Date.now().toString(36) + '-' + Math.random().toString(36).slice(2) + '-' + Math.random().toString(36).slice(2));

// --- data model --------------------------------------------------------------

function items() {
  const out = [];
  for (const m of data.messages) {
    out.push({
      dir: 'in', id: m.id, ids: m.ids, addr: m.sender, text: m.text, read: m.read, parts: m.parts, complete: m.complete,
      forward: m.forward, error: m.decode_error, time: Date.parse(m.timestamp) || m.received_at * 1000,
    });
  }
  for (const op of data.sent) {
    const req = op.request || {};
    out.push({
      dir: 'out', id: op.id, addr: req.number, kind: op.kind, status: op.status, result: op.result?.text,
      text: op.kind === 'send' ? req.text : (req.parts || []).join('\n'), from: req.sender, via: req.via,
      time: op.added * 1000, read: true,
    });
  }
  return out;
}

function threads(all) {
  const map = new Map();
  for (const it of all) {
    const key = threadKey(it.addr);
    const t = map.get(key) || { key, name: it.addr, items: [], unread: 0, last: null };
    t.items.push(it);
    if (it.dir === 'in' && !it.read) t.unread += 1;
    if (!t.last || it.time > t.last.time) t.last = it;
    // Prefer the sender spelling of incoming messages, it is what the operator shows.
    if (it.dir === 'in' && (!t.nameFromIn || it.time > t.nameTime)) Object.assign(t, { name: it.addr, nameFromIn: true, nameTime: it.time });
    map.set(key, t);
  }
  for (const t of map.values()) t.items.sort((a, b) => a.time - b.time);
  return [...map.values()];
}

function matches(text) {
  const q = ui.q.trim().toLowerCase();
  return !q || String(text || '').toLowerCase().includes(q);
}

// --- rendering: list -----------------------------------------------------------

function renderList(all, allThreads) {
  const list = $('list');
  const scroll = list.scrollTop;
  list.replaceChildren();
  let count = 0;

  if (ui.group === 'threads') {
    let ts = allThreads.filter((t) => (ui.filter !== 'unread' || t.unread > 0)
      && (matches(t.name) || t.items.some((i) => matches(i.text))));
    ts.sort(ui.sort === 'name' ? (a, b) => String(a.name).localeCompare(String(b.name), 'ru')
      : ui.sort === 'old' ? (a, b) => a.last.time - b.last.time : (a, b) => b.last.time - a.last.time);
    for (const t of ts) list.append(threadRow(t));
    count = ts.length;
    $('list-count').textContent = count ? `Диалогов: ${count}` : '';
  } else {
    let msgs = all.filter((i) => (ui.filter !== 'unread' || (i.dir === 'in' && !i.read)) && (matches(i.text) || matches(i.addr)));
    msgs.sort(ui.sort === 'name' ? (a, b) => String(a.addr).localeCompare(String(b.addr), 'ru') || b.time - a.time
      : ui.sort === 'old' ? (a, b) => a.time - b.time : (a, b) => b.time - a.time);
    let lastDay = null;
    for (const m of msgs) {
      if (ui.group === 'days' && ui.sort !== 'name') {
        const day = startOfDay(m.time);
        if (day !== lastDay) { list.append(h('div', { class: 'day', text: dayLabel(m.time) })); lastDay = day; }
      }
      list.append(messageRow(m));
    }
    count = msgs.length;
    $('list-count').textContent = count ? `Сообщений: ${count}` : '';
  }
  if (!count) {
    list.append(h('div', { class: 'empty', text: ui.q ? 'Ничего не найдено' : ui.filter === 'unread' ? 'Непрочитанных нет' : 'Сообщений пока нет' }));
  }
  list.scrollTop = scroll;
}

function snippet(it) {
  if (it.dir === 'out') return (it.kind === 'send' ? 'Вы: ' : '↪ ') + it.text;
  return it.text;
}

function threadRow(t) {
  const active = ui.selected === t.key;
  return h('button', {
    class: 'item' + (t.unread ? ' unread' : '') + (active ? ' active' : ''), type: 'button', role: 'listitem',
    'aria-current': active ? 'true' : null, onclick: () => openThread(t.key),
  },
  avatar(t.name, t.key),
  h('div', { class: 'item-main' },
    h('div', { class: 'item-top' }, h('span', { class: 'item-name', text: t.name })),
    h('div', { class: 'item-snippet', text: snippet(t.last) })),
  h('div', { class: 'item-side' },
    h('span', { class: 'item-time', text: shortTime(t.last.time), title: fullTime(t.last.time) }),
    t.unread ? h('b', { class: 'badge', text: t.unread }) : null));
}

function messageRow(m) {
  const key = threadKey(m.addr);
  const unread = m.dir === 'in' && !m.read;
  return h('button', {
    class: 'item' + (unread ? ' unread' : ''), type: 'button', role: 'listitem',
    onclick: () => openThread(key, m.id),
  },
  avatar(m.addr, key),
  h('div', { class: 'item-main' },
    h('div', { class: 'item-top' }, h('span', { class: 'item-name', text: m.dir === 'out' ? '→ ' + m.addr : m.addr })),
    h('div', { class: 'item-snippet', text: m.text })),
  h('div', { class: 'item-side' },
    h('span', { class: 'item-time', text: shortTime(m.time), title: fullTime(m.time) }),
    m.dir === 'in' && m.forward?.status === 'done' ? h('span', { class: 'tag', title: 'Переслано' }, icon('forward')) : null,
    unread ? h('b', { class: 'badge', text: '•' }) : null));
}

// --- rendering: conversation -----------------------------------------------------

const STATUS = { done: 'отправлено', error: 'ошибка', pending: 'отправка…', unknown: 'результат неизвестен' };
const FWD = { done: 'переслано', error: 'ошибка пересылки', pending: 'пересылается…', skipped: 'не пересылается (свой номер)', limit: 'не переслано: лимит' };

function renderDetail(allThreads) {
  const t = allThreads.find((x) => x.key === ui.selected);
  const shown = Boolean(t);
  $('empty-detail').hidden = shown;
  $('detail-head').hidden = !shown;
  $('composer').hidden = !shown || !isPhone(t?.name) && !t?.items.some((i) => isPhone(i.addr));
  const conv = $('conversation');
  if (!shown) {
    conv.replaceChildren();
    return;
  }
  $('detail-avatar').replaceWith(Object.assign(avatar(t.name, t.key), { id: 'detail-avatar' }));
  $('detail-name').textContent = t.name;
  $('detail-sub').textContent = `${t.items.filter((i) => i.dir === 'in').length} входящих · ${t.items.filter((i) => i.dir === 'out').length} исходящих`;
  $('thread-unread').hidden = !t.items.some((i) => i.dir === 'in');

  const nearBottom = conv.scrollHeight - conv.scrollTop - conv.clientHeight < 80;
  const prevScroll = conv.scrollTop;
  conv.replaceChildren();
  let lastDay = null;
  for (const it of t.items) {
    const day = startOfDay(it.time);
    if (day !== lastDay) { conv.append(h('div', { class: 'sep', text: dayLabel(it.time) })); lastDay = day; }
    conv.append(bubble(it));
  }
  if (ui.highlight != null) {
    const target = conv.querySelector(`[data-id="${CSS.escape(String(ui.highlight))}"]`);
    if (target) { target.classList.add('hl'); target.scrollIntoView({ block: 'center' }); }
    ui.highlight = null;
  } else if (nearBottom || ui.scrollToEnd) {
    conv.scrollTop = conv.scrollHeight;
  } else {
    conv.scrollTop = prevScroll;
  }
  ui.scrollToEnd = false;
}

function bubble(it) {
  const pending = busyNow();
  if (it.dir === 'out') {
    const label = it.kind === 'forward' ? h('div', { class: 'label' }, icon('forward'), `Пересылка SMS от ${it.from || '—'}`)
      : it.kind === 'forward-test' ? h('div', { class: 'label' }, icon('forward'), 'Тест пересылки') : null;
    return h('div', { class: `bubble out${it.status === 'error' ? ' err' : ''}${it.status === 'pending' ? ' pending' : ''}`, 'data-id': it.id },
      label,
      h('div', { class: 'text', text: it.text }),
      h('div', { class: 'meta' },
        String(it.via || '').startsWith('api:') ? h('span', { text: 'API' }) : null,
        h('span', { text: STATUS[it.status] || it.status, title: it.result || '' }),
        h('span', { text: hhmm(it.time), title: fullTime(it.time) })),
      it.status === 'error' && it.result ? h('div', { class: 'meta', text: it.result }) : null);
  }
  const fwd = it.forward ? FWD[it.forward.status] : null;
  const canForward = Boolean(data.forwarding.number);
  return h('div', { class: `bubble in${it.read ? '' : ' unread'}`, 'data-id': it.id },
    h('div', { class: 'text', text: it.text }),
    h('div', { class: 'meta' },
      it.parts ? h('span', { text: it.complete ? `частей: ${it.parts}` : `получено частей: ${it.parts}` }) : null,
      fwd ? h('span', { class: 'fwd-state', text: fwd }) : null,
      h('span', { text: hhmm(it.time), title: fullTime(it.time) }),
      h('span', { class: 'bubble-actions' },
        h('button', { type: 'button', title: 'Копировать', 'aria-label': 'Копировать текст', onclick: () => copy(it.text) }, icon('copy')),
        canForward ? h('button', { type: 'button', disabled: pending, title: 'Переслать на ' + data.forwarding.number, onclick: () => forwardOne(it) }, icon('forward'), 'Переслать') : null,
        h('button', { type: 'button', onclick: () => setRead(it.ids, !it.read, true), text: it.read ? 'Непрочитано' : 'Прочитано' }))));
}

// --- rendering: pages --------------------------------------------------------------

const KIND = {
  send: (r) => `SMS → ${r.number}`,
  forward: (r) => `Пересылка → ${r.number}${r.sender ? ' · от ' + r.sender : ''}`,
  'forward-test': (r) => `Тест пересылки → ${r.number}`,
  ussd: (r) => `USSD ${r.code}`,
  sync: () => 'Обновление',
  clear: () => 'Очистка SIM',
};
const OP_PILL = { done: ['ok', 'готово'], error: ['err', 'ошибка'], pending: ['warn', 'в работе'], unknown: ['warn', 'неизвестно'] };

function opRow(op) {
  const req = op.request || {};
  const [cls, label] = OP_PILL[op.status] || ['', op.status];
  const body = [];
  if (op.kind === 'send') body.push(req.text);
  if (op.kind === 'forward' || op.kind === 'forward-test') body.push((req.parts || []).join('\n'));
  if (op.status === 'pending') body.push('Выполняется…');
  else if (op.result?.text) body.push((body.length ? '→ ' : '') + op.result.text);
  const via = String(req.via || '');
  return h('div', { class: 'op' },
    h('span', { class: 'pill ' + cls, text: label }),
    h('div', { class: 'op-title' }, h('span', { text: (KIND[op.kind] || (() => op.kind))(req) + (via.startsWith('api:') ? ` · API ${via.slice(4)}` : req.auto ? ' · авто' : '') }),
      h('small', { text: fullTime(op.added * 1000) })),
    body.length ? h('div', { class: 'op-body', text: body.join('\n') }) : null);
}

function renderOps(el, ops, empty) {
  el.replaceChildren(...(ops.length ? ops.map(opRow) : [h('p', { class: 'muted', text: empty })]));
}

function renderPages() {
  const ops = data.operations;
  renderOps($('ussd-history'), ops.filter((o) => o.kind === 'ussd').slice(0, 8), 'Запросов пока не было');
  renderOps($('journal'), ops, 'Операций пока нет');
  renderOps($('fwd-history'), data.sent.filter((o) => o.kind !== 'send').slice(0, 6), 'Пересылок пока не было');

  const f = data.forwarding;
  const pill = $('fwd-pill');
  pill.className = 'pill ' + (f.enabled ? 'ok' : '');
  pill.textContent = f.enabled ? `включена → ${f.number}` : 'выключена';
  if (!fwdDirty) {
    $('fwd-enabled').checked = f.enabled;
    $('fwd-number').value = f.number || '';
    $('fwd-parts').value = String(f.max_parts || 3);
  }

  const s = data.state;
  $('sim-big').textContent = `${s.used ?? '—'} / ${s.capacity ?? '—'}`;
  const pct = s.capacity ? Math.min(100, (100 * (s.used || 0)) / s.capacity) : 0;
  for (const id of ['sim-meter', 'sim-meter-big']) {
    $(id).style.width = pct + '%';
    $(id).parentElement.classList.toggle('full', pct >= 90);
  }

  const l = data.limits;
  $('limit-big').textContent = l.monthly ? `${l.used} / ${l.monthly}` : String(l.used);
  $('limit-sub').textContent = l.monthly ? 'SMS отправлено в этом периоде' : 'SMS отправлено в этом периоде, без ограничения';
  const lpct = l.monthly ? Math.min(100, (100 * l.used) / l.monthly) : 0;
  $('limit-meter').style.width = lpct + '%';
  $('limit-meter').parentElement.classList.toggle('full', lpct >= 90);
  $('limit-meter').parentElement.hidden = !l.monthly;
  $('limit-note').textContent = `Период: с ${dateOnly(l.period_start * 1000)} по ${dateOnly(l.next_reset * 1000 - 1)}. Обновление ${dateOnly(l.next_reset * 1000)}.`;
  const lp = $('limit-pill');
  lp.className = 'pill ' + (!l.monthly ? '' : l.remaining === 0 ? 'err' : lpct >= 80 ? 'warn' : 'ok');
  lp.textContent = !l.monthly ? 'без лимита' : l.remaining === 0 ? 'исчерпан' : `осталось ${l.remaining}`;
  if (!limitDirty) {
    $('limit-monthly').value = String(l.monthly);
    $('limit-day').value = String(l.reset_day);
  }

  const api = $('api-pill');
  api.className = 'pill ' + (data.api_enabled ? 'ok' : 'warn');
  api.textContent = data.api_enabled ? 'токены выпущены' : 'токенов нет';
  $('api-example').textContent = `TOKEN=...\ncurl -H "Authorization: Bearer $TOKEN" "${location.origin}/api/v1/messages?unread=1"\n`
    + `curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \\\n  -d '{"number":"+79990000000","text":"Hello"}' ${location.origin}/api/v1/send`;
}

function renderStatus() {
  const s = data.state;
  $('status-dot').className = 'dot ' + (s.error ? 'err' : s.last_sync ? 'ok' : '');
  $('status-text').textContent = s.error ? 'Нет связи с модемом' : s.last_sync ? 'MikroTik на связи' : 'Ожидание опроса…';
  $('status-card').title = (s.error ? 'Ошибка: ' + s.error + '\n' : '') + 'Последний опрос: ' + fullTime(s.last_sync * 1000) + (s.forward_note ? '\n' + s.forward_note : '');
  $('brand-sub').textContent = s.last_sync ? 'обновлено ' + hhmm(s.last_sync * 1000) : 'MikroTik · T2';
  $('sim-text').textContent = `SIM: ${s.used ?? '—'} / ${s.capacity ?? '—'}${s.capacity && s.used >= s.capacity ? ' — заполнена' : ''}`;
  const l = data.limits;
  $('limit-text').hidden = !l.monthly;
  $('limit-text').textContent = l.monthly ? `SMS за период: ${l.used} / ${l.monthly}` : '';
  const unread = data.messages.filter((m) => !m.read).length;
  for (const id of ['nav-unread', 'tab-unread']) {
    $(id).hidden = !unread;
    $(id).textContent = unread > 99 ? '99+' : unread;
  }
  $('chip-unread').textContent = unread ? unread : '';
  $('read-all').hidden = !unread;
  document.title = unread ? `(${unread}) SMS Gateway` : 'SMS Gateway';
  const disabled = busyNow();
  for (const el of document.querySelectorAll('#composer-send, #compose-send, [data-ussd], #ussd-form button, #sim-clear, #fwd-test, #sync')) el.disabled = disabled;
}

const busyNow = () => busy || Boolean(data?.operations.some((o) => o.status === 'pending'));

function render(force = false) {
  if (!data) return;
  renderStatus();
  const signature = JSON.stringify([data.messages, data.sent, data.operations, data.forwarding, data.limits, ui.group, ui.filter, ui.sort, ui.q, ui.selected, busyNow()]);
  if (!force && signature === lastSignature) return;
  lastSignature = signature;
  let all = items();
  let allThreads = threads(all);
  if (autoRead(allThreads)) {
    all = items();
    allThreads = threads(all);
    renderStatus();
  }
  renderList(all, allThreads);
  renderDetail(allThreads);
  renderPages();
}

// --- actions ---------------------------------------------------------------------

async function post(path, body, retry = true) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (r.status === 403 && retry) {
    await refresh();
    return post(path, body, false);
  }
  if (!r.ok) throw new Error(d.error || 'Ошибка ' + r.status);
  return d;
}

async function refresh() {
  try {
    const r = await fetch('/api/state', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    data = await r.json();
    csrf = data.csrf;
    render();
  } catch (e) {
    $('status-dot').className = 'dot err';
    $('status-text').textContent = 'Нет связи с сервисом';
    $('status-card').title = e.message;
  }
}

async function action(kind, payload, accepted = 'Запрос принят, результат появится в журнале') {
  if (busyNow()) return false;
  busy = true;
  render(true);
  try {
    await post('/api/' + kind, { ...payload, id: operationId() });
    toast(accepted);
    return true;
  } catch (e) {
    toast(e.message + '. Если связь оборвалась после отправки, проверьте журнал перед повтором.', true);
    return false;
  } finally {
    busy = false;
    await refresh();
    render(true);
  }
}

async function setRead(ids, read, manual = false) {
  if (!ids.length) return;
  for (const id of ids) {
    if (manual && !read) ui.keepUnread.add(id); else ui.keepUnread.delete(id);
  }
  for (const m of data.messages) if (m.ids.some((i) => ids.includes(i))) m.read = read;
  render(true);
  try {
    await post('/api/read', { ids, read });
  } catch (e) {
    toast(e.message, true);
  }
  refresh();
}

function autoRead(allThreads) {
  const t = allThreads.find((x) => x.key === ui.selected);
  const visible = ui.view === 'messages' && document.visibilityState === 'visible'
    && (matchMedia('(min-width: 821px)').matches || $('app').classList.contains('show-detail'));
  if (!t || !visible) return false;
  const ids = t.items.filter((i) => i.dir === 'in' && !i.read && !i.ids.some((x) => ui.keepUnread.has(x))).flatMap((i) => i.ids);
  if (!ids.length) return false;
  for (const m of data.messages) if (m.ids.some((i) => ids.includes(i))) m.read = true;
  post('/api/read', { ids, read: true }).catch((e) => toast(e.message, true));
  return true;
}

function openThread(key, highlight = null) {
  if (ui.selected !== key) ui.keepUnread.clear();
  ui.selected = key;
  ui.highlight = highlight;
  ui.scrollToEnd = highlight == null;
  $('app').classList.add('show-detail');
  render(true);
}

function closeThread() {
  $('app').classList.remove('show-detail');
  if (matchMedia('(max-width: 820px)').matches) setTimeout(() => { ui.selected = null; render(true); }, 220);
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('Текст скопирован');
  } catch {
    toast('Не удалось скопировать', true);
  }
}

function forwardOne(it) {
  const number = data.forwarding.number;
  if (confirm(`Переслать сообщение от ${it.addr} на ${number}? Будет отправлено SMS по тарифу оператора.`)) {
    action('forward', { message_id: it.id }, 'Пересылка поставлена в очередь');
  }
}

function checkSms(number, text) {
  const { units, invalid } = gsmInfo(text);
  if (!isPhone(number)) return 'Номер: от 3 до 15 цифр, допустим + в начале';
  if (!text.trim()) return 'Введите текст SMS';
  if (invalid) return 'В тексте есть символы вне GSM7 — нажмите «В транслит» и проверьте результат';
  if (units > 160) return 'Больше 160 единиц GSM7';
  return null;
}

async function sendSms(number, text) {
  const problem = checkSms(number, text);
  if (problem) { toast(problem, true); return false; }
  if (!confirm(`Отправить SMS на ${number}?`)) return false;
  return action('send', { number, text }, 'SMS передано модему');
}

function threadNumber() {
  const t = threads(items()).find((x) => x.key === ui.selected);
  const it = t && [...t.items].reverse().find((i) => isPhone(i.addr));
  return it ? String(it.addr).replace(/[\s()-]/g, '') : '';
}

function setView(view) {
  ui.view = view;
  $('app').dataset.view = view;
  for (const v of ['messages', 'ussd', 'journal', 'settings']) $('view-' + v).hidden = v !== view;
  for (const b of document.querySelectorAll('[data-nav]')) b.classList.toggle('active', b.dataset.nav === view);
  render(true);
}

function setRadio(groupId, value) {
  for (const b of $(groupId).querySelectorAll('[data-value]')) b.setAttribute('aria-checked', String(b.dataset.value === value));
}

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}

function autosize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight + 2, 180) + 'px';
}

// --- wiring ----------------------------------------------------------------------

function init() {
  applyTheme(prefs.get('theme', ''));
  setRadio('group', ui.group);
  setRadio('filter', ui.filter);
  $('sort').value = ui.sort;

  for (const b of document.querySelectorAll('[data-nav]')) b.addEventListener('click', () => setView(b.dataset.nav));
  $('group').addEventListener('click', (e) => {
    const v = e.target.closest('[data-value]')?.dataset.value;
    if (v) { ui.group = v; prefs.set('group', v); setRadio('group', v); render(true); }
  });
  $('filter').addEventListener('click', (e) => {
    const v = e.target.closest('[data-value]')?.dataset.value;
    if (v) { ui.filter = v; prefs.set('filter', v); setRadio('filter', v); render(true); }
  });
  $('sort').addEventListener('change', () => { ui.sort = $('sort').value; prefs.set('sort', ui.sort); render(true); });
  $('search').addEventListener('input', () => { ui.q = $('search').value; render(true); });
  $('read-all').addEventListener('click', () => {
    const ids = data.messages.filter((m) => !m.read).flatMap((m) => m.ids);
    setRead(ids, true);
  });
  $('back').addEventListener('click', closeThread);
  $('thread-unread').addEventListener('click', () => {
    const t = threads(items()).find((x) => x.key === ui.selected);
    const last = t && [...t.items].reverse().find((i) => i.dir === 'in');
    if (!last) return;
    setRead(last.ids, false, true);
    closeThread();
  });

  $('sync').addEventListener('click', () => action('sync', {}, 'Проверяю SIM…'));
  $('theme').addEventListener('click', () => {
    const dark = getComputedStyle(document.documentElement).colorScheme.includes('dark');
    const next = dark ? 'light' : 'dark';
    prefs.set('theme', next);
    applyTheme(next);
  });

  const updateComposer = bindCounter($('composer-text'), $('composer-count'));
  $('composer-text').addEventListener('input', () => autosize($('composer-text')));
  $('composer-translit').addEventListener('click', () => { $('composer-text').value = translit($('composer-text').value); updateComposer(); });
  $('composer').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (await sendSms(threadNumber(), $('composer-text').value)) {
      $('composer-text').value = '';
      updateComposer();
      autosize($('composer-text'));
      ui.scrollToEnd = true;
    }
  });
  $('composer-text').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) $('composer').requestSubmit();
  });

  const dialog = $('compose-dialog');
  const updateCompose = bindCounter($('compose-text'), $('compose-count'));
  const openCompose = () => {
    $('compose-number').value = threadNumber();
    dialog.showModal();
    ($('compose-number').value ? $('compose-text') : $('compose-number')).focus();
  };
  $('compose-open').addEventListener('click', openCompose);
  $('compose-fab').addEventListener('click', openCompose);
  $('compose-close').addEventListener('click', () => dialog.close());
  $('compose-translit').addEventListener('click', () => { $('compose-text').value = translit($('compose-text').value); updateCompose(); });
  $('compose-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const number = $('compose-number').value.trim();
    if (await sendSms(number, $('compose-text').value)) {
      dialog.close();
      $('compose-text').value = '';
      updateCompose();
      setView('messages');
      openThread(threadKey(number));
    }
  });

  for (const b of document.querySelectorAll('[data-ussd]')) b.addEventListener('click', () => action('ussd', { code: b.dataset.ussd }, 'USSD отправлен, ответ может занять до 2 минут'));
  $('ussd-form').addEventListener('submit', (e) => {
    e.preventDefault();
    action('ussd', { code: $('ussd-code').value.trim() }, 'USSD отправлен, ответ может занять до 2 минут');
  });
  $('sim-clear').addEventListener('click', () => {
    if (confirm('Удалить с SIM уже сохранённые сообщения? Архив на роутере останется.')) action('clear', { confirm: true }, 'Проверяю SIM…');
  });

  for (const id of ['fwd-enabled', 'fwd-number', 'fwd-parts']) $(id).addEventListener('input', () => { fwdDirty = true; });

  for (let d = 1; d <= 28; d += 1) $('limit-day').append(h('option', { value: String(d), text: `${d}-го числа каждого месяца` }));
  for (const id of ['limit-monthly', 'limit-day']) $(id).addEventListener('input', () => { limitDirty = true; });
  $('limit-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      data.limits = await post('/api/limits', { monthly: Number($('limit-monthly').value), reset_day: Number($('limit-day').value) });
      limitDirty = false;
      toast(data.limits.monthly ? `Лимит: ${data.limits.monthly} SMS в месяц` : 'Лимит отключён');
      render(true);
    } catch (err) {
      toast(err.message, true);
    }
  });
  $('fwd-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const enabled = $('fwd-enabled').checked;
    const number = $('fwd-number').value.trim();
    if (enabled && !data.forwarding.enabled && !confirm(`Включить пересылку всех новых SMS на ${number}? Каждая пересылка — платное SMS.`)) return;
    try {
      data.forwarding = await post('/api/forwarding', { enabled, number, max_parts: Number($('fwd-parts').value) });
      fwdDirty = false;
      toast(enabled ? 'Пересылка включена' : 'Настройки сохранены');
      render(true);
    } catch (err) {
      toast(err.message, true);
    }
  });
  $('fwd-test').addEventListener('click', () => {
    const number = $('fwd-number').value.trim();
    if (!isPhone(number)) { toast('Укажите номер получателя', true); return; }
    if (confirm(`Отправить тестовую пересылку на ${number}? Будет отправлено платное SMS.`)) {
      action('forward-test', { number }, 'Тестовая пересылка отправляется');
    }
  });

  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refresh(); });
  refresh();
  setInterval(() => { if (document.visibilityState === 'visible') refresh(); }, 5000);
}

document.addEventListener('DOMContentLoaded', init);
