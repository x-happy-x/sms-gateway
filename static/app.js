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
  selecting: false,
  sel: new Set(),
  anchor: null,
  order: [],
  selectable: new Set(),
  suppressClick: 0,
  contactQ: '',
  editContact: null,
};
let current = { items: [], threads: [] };
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

const digits10 = (addr) => String(addr || '').replace(/\D/g, '').slice(-10);
let contactIndex = new Map();

function indexContacts() {
  contactIndex = new Map((data.contacts || []).map((c) => [digits10(c.number), c]));
}

function contactFor(addr) {
  return isPhone(addr) ? contactIndex.get(digits10(addr)) || null : null;
}

const displayName = (addr) => contactFor(addr)?.name || addr || '—';

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
    const text = op.kind === 'send' ? req.text
      : op.kind === 'forward-batch' ? (req.items || []).map((i) => i.parts.join('\n')).join('\n\n')
        : (req.parts || []).join('\n');
    out.push({
      dir: 'out', id: op.id, addr: req.number, kind: op.kind, status: op.status, result: op.result?.text,
      text, from: req.sender, count: req.count, via: req.via, time: op.added * 1000, read: true, delivery: op.delivery,
    });
  }
  return out;
}

function threads(all) {
  const map = new Map();
  for (const it of all) {
    const key = threadKey(it.addr);
    const t = map.get(key) || { key, addr: it.addr, items: [], unread: 0, last: null };
    t.items.push(it);
    if (it.dir === 'in' && !it.read) t.unread += 1;
    if (!t.last || it.time > t.last.time) t.last = it;
    // Prefer the sender spelling of incoming messages, it is what the operator shows.
    if (it.dir === 'in' && (!t.fromIn || it.time > t.addrTime)) Object.assign(t, { addr: it.addr, fromIn: true, addrTime: it.time });
    map.set(key, t);
  }
  for (const t of map.values()) {
    t.items.sort((a, b) => a.time - b.time);
    t.contact = contactFor(t.addr);
    t.name = t.contact?.name || t.addr;
  }
  return [...map.values()];
}

function matches(text) {
  const q = ui.q.trim().toLowerCase();
  return !q || String(text || '').toLowerCase().includes(q);
}

// --- selection -------------------------------------------------------------------

function enterSelect() {
  ui.selecting = true;
  ui.sel.clear();
  ui.anchor = null;
}

function exitSelect() {
  ui.selecting = false;
  ui.sel.clear();
  ui.anchor = null;
  render(true);
}

function selectRange(from, to) {
  const [a, b] = from < to ? [from, to] : [to, from];
  for (let i = a; i <= b; i += 1) if (ui.selectable.has(ui.order[i])) ui.sel.add(ui.order[i]);
}

function selectableRow({ key, selectable, open, cls }, leading, ...rest) {
  const index = ui.order.push(key) - 1;
  if (selectable) ui.selectable.add(key);
  const selected = ui.sel.has(key);
  const state = !ui.selecting ? '' : !selectable ? ' not-selectable' : selected ? ' selected' : '';
  const el = h('button', {
    class: 'item' + cls + state, type: 'button', role: ui.selecting ? 'checkbox' : 'listitem',
    'aria-checked': ui.selecting ? String(selected) : null,
  }, ui.selecting ? h('span', { class: 'check', 'aria-hidden': 'true' }, icon('check')) : leading, ...rest);

  el.addEventListener('click', (e) => {
    if (Date.now() < ui.suppressClick) return;
    if (!ui.selecting && !(e.ctrlKey || e.metaKey)) { open(); return; }
    if (!selectable) return;
    if (!ui.selecting) enterSelect();
    if (e.shiftKey && ui.anchor != null) selectRange(ui.anchor, index);
    else if (ui.sel.has(key)) ui.sel.delete(key);
    else ui.sel.add(key);
    ui.anchor = index;
    render(true);
  });

  let timer = null;
  let start = null;
  el.addEventListener('pointerdown', (e) => {
    if (e.pointerType !== 'touch' || !selectable) return;
    start = [e.clientX, e.clientY];
    timer = setTimeout(() => {
      timer = null;
      ui.suppressClick = Date.now() + 600;
      if (!ui.selecting) enterSelect();
      ui.sel.add(key);
      ui.anchor = index;
      navigator.vibrate?.(12);
      render(true);
    }, 450);
  });
  const cancel = (e) => {
    if (!timer) return;
    if (e.type === 'pointermove' && Math.hypot(e.clientX - start[0], e.clientY - start[1]) < 8) return;
    clearTimeout(timer);
    timer = null;
  };
  for (const type of ['pointerup', 'pointercancel', 'pointerleave', 'pointermove']) el.addEventListener(type, cancel);
  return el;
}

function selectedMessages() {
  const out = new Map();
  for (const key of ui.sel) {
    if (key.startsWith('t:')) {
      const t = current.threads.find((x) => 't:' + x.key === key);
      for (const i of t ? t.items : []) if (i.dir === 'in') out.set(i.id, i);
    } else {
      const it = current.items.find((i) => i.dir === 'in' && 'm:' + i.id === key);
      if (it) out.set(it.id, it);
    }
  }
  return [...out.values()];
}

// --- rendering: list -----------------------------------------------------------

function renderList(all, allThreads) {
  const list = $('list');
  const scroll = list.scrollTop;
  list.replaceChildren();
  list.classList.toggle('selecting', ui.selecting);
  ui.order = [];
  ui.selectable = new Set();
  let count = 0;

  if (ui.group === 'threads') {
    const ts = allThreads.filter((t) => (ui.filter !== 'unread' || t.unread > 0)
      && (matches(t.name) || matches(t.addr) || t.items.some((i) => matches(i.text))));
    ts.sort(ui.sort === 'name' ? (a, b) => String(a.name).localeCompare(String(b.name), 'ru')
      : ui.sort === 'old' ? (a, b) => a.last.time - b.last.time : (a, b) => b.last.time - a.last.time);
    for (const t of ts) list.append(threadRow(t));
    count = ts.length;
    $('list-count').textContent = count ? `Диалогов: ${count}` : '';
  } else {
    const msgs = all.filter((i) => (ui.filter !== 'unread' || (i.dir === 'in' && !i.read))
      && (matches(i.text) || matches(i.addr) || matches(displayName(i.addr))));
    msgs.sort(ui.sort === 'name' ? (a, b) => String(displayName(a.addr)).localeCompare(String(displayName(b.addr)), 'ru') || b.time - a.time
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
  // Hidden rows (search, filter) drop out of the selection so actions only touch what is visible.
  for (const key of [...ui.sel]) if (!ui.selectable.has(key)) ui.sel.delete(key);
  list.scrollTop = scroll;
  renderBulk();
}

function renderBulk() {
  $('bulk-bar').hidden = !ui.selecting;
  $('select-toggle').textContent = ui.selecting ? 'Готово' : 'Выбрать';
  $('read-all').hidden = ui.selecting || !data.messages.some((m) => !m.read);
  if (!ui.selecting) return;
  const msgs = selectedMessages();
  const n = msgs.length;
  $('bulk-count').textContent = !ui.sel.size ? 'Ничего не выбрано'
    : ui.group === 'threads' ? `Отправителей: ${ui.sel.size} · сообщений: ${n}` : `Выбрано: ${n}`;
  const all = ui.selectable.size > 0 && [...ui.selectable].every((k) => ui.sel.has(k));
  $('bulk-all').textContent = all ? 'Снять все' : 'Выбрать все';
  $('bulk-read').disabled = !msgs.some((m) => !m.read);
  $('bulk-unread').disabled = !msgs.some((m) => m.read);
  $('bulk-forward').disabled = !n || busyNow();
  $('bulk-forward').title = data.forwarding.number ? `Переслать на ${displayName(data.forwarding.number)}` : 'Номер пересылки не задан';
  $('bulk-delete').disabled = !n;
}

function snippet(it) {
  if (it.dir === 'out') return (it.kind === 'send' ? 'Вы: ' : '↪ ') + it.text;
  return it.text;
}

function threadRow(t) {
  const active = ui.selected === t.key && !ui.selecting;
  return selectableRow({
    key: 't:' + t.key, selectable: t.items.some((i) => i.dir === 'in'), open: () => openThread(t.key),
    cls: (t.unread ? ' unread' : '') + (active ? ' active' : ''),
  },
  avatar(t.name, t.key),
  h('div', { class: 'item-main' },
    h('div', { class: 'item-top' }, h('span', { class: 'item-name', text: t.name }),
      t.contact ? h('span', { class: 'item-sub', text: t.addr }) : null),
    h('div', { class: 'item-snippet', text: snippet(t.last) })),
  h('div', { class: 'item-side' },
    h('span', { class: 'item-time', text: shortTime(t.last.time), title: fullTime(t.last.time) }),
    t.unread ? h('b', { class: 'badge', text: t.unread }) : null));
}

function messageRow(m) {
  const key = threadKey(m.addr);
  const unread = m.dir === 'in' && !m.read;
  const name = displayName(m.addr);
  return selectableRow({
    key: 'm:' + m.id, selectable: m.dir === 'in', open: () => openThread(key, m.id), cls: unread ? ' unread' : '',
  },
  avatar(name, key),
  h('div', { class: 'item-main' },
    h('div', { class: 'item-top' }, h('span', { class: 'item-name', text: m.dir === 'out' ? '→ ' + name : name })),
    h('div', { class: 'item-snippet', text: m.text })),
  h('div', { class: 'item-side' },
    h('span', { class: 'item-time', text: shortTime(m.time), title: fullTime(m.time) }),
    m.dir === 'in' && m.forward?.status === 'done' ? h('span', { class: 'tag', title: 'Переслано' }, icon('forward')) : null,
    unread ? h('b', { class: 'badge', text: '•' }) : null));
}

// --- rendering: conversation -----------------------------------------------------

const STATUS = { done: 'отправлено', error: 'ошибка', pending: 'отправка…', unknown: 'результат неизвестен' };

function deliveryLabel(status, d) {
  if (status !== 'done' || !d) return { text: STATUS[status] || status, cls: '' };
  const partial = d.total > 1 ? ` ${d.delivered}/${d.total}` : '';
  if (d.state === 'delivered') return { text: 'доставлено ✓✓', cls: 'ok', title: d.at ? 'Доставлено ' + fullTime(Date.parse(d.at)) : '' };
  if (d.state === 'failed') return { text: 'не доставлено' + partial, cls: 'err', title: d.text || '' };
  if (d.state === 'pending') return { text: d.delivered ? 'доставлено' + partial : 'отправлено · ждём отчёт', cls: '', title: d.text || 'Отчёт о доставке ещё не пришёл' };
  return { text: 'отправлено', cls: '', title: 'Отчёт о доставке не пришёл' };
}
const FWD = { done: 'переслано', error: 'ошибка пересылки', pending: 'пересылается…', skipped: 'не пересылается (свой номер)', limit: 'не переслано: лимит' };

function renderDetail(allThreads) {
  const t = allThreads.find((x) => x.key === ui.selected);
  const shown = Boolean(t);
  $('empty-detail').hidden = shown;
  $('detail-head').hidden = !shown;
  $('composer').hidden = !shown || !t.items.some((i) => isPhone(i.addr));
  const conv = $('conversation');
  if (!shown) {
    conv.replaceChildren();
    return;
  }
  $('detail-avatar').replaceWith(Object.assign(avatar(t.name, t.key), { id: 'detail-avatar' }));
  $('detail-name').textContent = t.name;
  const counts = `${t.items.filter((i) => i.dir === 'in').length} входящих · ${t.items.filter((i) => i.dir === 'out').length} исходящих`;
  $('detail-sub').textContent = t.contact ? `${t.addr} · ${counts}` : counts;
  $('thread-unread').hidden = !t.items.some((i) => i.dir === 'in');
  $('thread-contact').hidden = !isPhone(t.addr);
  $('thread-contact').textContent = t.contact ? 'Контакт' : 'В контакты';

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
      : it.kind === 'forward-batch' ? h('div', { class: 'label' }, icon('forward'), `Пересылка: ${it.count} ${plural(it.count, 'сообщение', 'сообщения', 'сообщений')}`)
        : it.kind === 'forward-test' ? h('div', { class: 'label' }, icon('forward'), 'Тест пересылки') : null;
    return h('div', { class: `bubble out${it.status === 'error' ? ' err' : ''}${it.status === 'pending' ? ' pending' : ''}`, 'data-id': it.id },
      label,
      h('div', { class: 'text', text: it.text }),
      h('div', { class: 'meta' },
        String(it.via || '').startsWith('api:') ? h('span', { text: 'API' }) : null,
        ((l) => h('span', { class: 'delivery ' + l.cls, text: l.text, title: l.title || it.result || '' }))(deliveryLabel(it.status, it.delivery)),
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
        canForward ? h('button', { type: 'button', disabled: pending, title: 'Переслать на ' + displayName(data.forwarding.number), onclick: () => forwardOne(it) }, icon('forward'), 'Переслать') : null,
        h('button', { type: 'button', onclick: () => setRead(it.ids, !it.read, true), text: it.read ? 'Непрочитано' : 'Прочитано' }))));
}

// --- rendering: pages --------------------------------------------------------------

const KIND = {
  send: (r) => `SMS → ${displayName(r.number)}`,
  forward: (r) => `Пересылка → ${displayName(r.number)}${r.sender ? ' · от ' + r.sender : ''}`,
  'forward-batch': (r) => `Пересылка ${r.count} ${plural(r.count, 'сообщения', 'сообщений', 'сообщений')} → ${displayName(r.number)}`,
  'forward-test': (r) => `Тест пересылки → ${displayName(r.number)}`,
  ussd: (r) => `USSD ${r.code}`,
  sync: () => 'Обновление',
  clear: () => 'Очистка SIM',
};
const OP_PILL = { done: ['ok', 'готово'], error: ['err', 'ошибка'], pending: ['warn', 'в работе'], unknown: ['warn', 'неизвестно'] };

function opRow(op) {
  const req = op.request || {};
  const d = op.status === 'done' ? op.delivery : null;
  const [cls, label] = d?.state === 'delivered' ? ['ok', 'доставлено'] : d?.state === 'failed' ? ['err', 'не доставлено']
    : OP_PILL[op.status] || ['', op.status];
  const body = [];
  if (op.kind === 'send') body.push(req.text);
  if (op.kind === 'forward' || op.kind === 'forward-test') body.push((req.parts || []).join('\n'));
  if (op.kind === 'forward-batch') body.push((req.items || []).map((i) => i.parts.join('\n')).join('\n\n'));
  if (op.status === 'pending') body.push('Выполняется…');
  else if (op.result?.text) body.push((body.length ? '→ ' : '') + op.result.text);
  if (d && d.state !== 'delivered') body.push('Доставка: ' + deliveryLabel(op.status, d).text + (d.text && d.state === 'failed' ? ` (${d.text})` : ''));
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
  pill.textContent = f.enabled ? `включена → ${displayName(f.number)}` : 'выключена';
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

  setRadio('theme-choice', prefs.get('theme', ''));
  $('contact-options').replaceChildren(...data.contacts.map((c) => h('option', { value: c.number, label: c.name })));
  renderContacts();

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
  document.title = unread ? `(${unread}) SMS Gateway` : 'SMS Gateway';
  const disabled = busyNow();
  for (const el of document.querySelectorAll('#composer-send, #compose-send, [data-ussd], #ussd-form button, #sim-clear, #fwd-test, #sync')) el.disabled = disabled;
}

function renderContacts() {
  const list = $('contacts-list');
  const q = ui.contactQ.trim().toLowerCase();
  const counts = new Map();
  for (const m of data.messages) {
    if (isPhone(m.sender)) counts.set(digits10(m.sender), (counts.get(digits10(m.sender)) || 0) + 1);
  }
  const shown = data.contacts.filter((c) => !q || [c.name, c.number, c.note].some((v) => String(v || '').toLowerCase().includes(q)))
    .sort((a, b) => a.name.localeCompare(b.name, 'ru', { sensitivity: 'base' }));
  $('contacts-count').textContent = data.contacts.length
    ? `${data.contacts.length} ${plural(data.contacts.length, 'контакт', 'контакта', 'контактов')}` : 'Контактов пока нет';
  list.replaceChildren();
  let letter = null;
  for (const c of shown) {
    const first = (c.name.trim()[0] || '#').toUpperCase();
    if (first !== letter) { list.append(h('div', { class: 'letter', text: first })); letter = first; }
    const key = 'n:' + digits10(c.number);
    const n = counts.get(digits10(c.number)) || 0;
    list.append(h('div', { class: 'contact' },
      avatar(c.name, key),
      h('button', { class: 'contact-main', type: 'button', onclick: () => openContact(c) },
        h('strong', { text: c.name }),
        h('small', { text: c.number + (c.note ? ' · ' + c.note : '') })),
      h('div', { class: 'contact-actions' },
        n ? h('button', { class: 'ghost sm', type: 'button', title: 'Открыть переписку', onclick: () => { setView('messages'); openThread(key); } },
          icon('chat'), String(n)) : null,
        h('button', { class: 'ghost sm icon', type: 'button', title: 'Написать', 'aria-label': `Написать ${c.name}`, onclick: () => openCompose(c.number) }, icon('send')))));
  }
  if (!shown.length) {
    list.append(h('div', { class: 'empty', text: q ? 'Ничего не найдено' : 'Импортируйте файл с контактами или добавьте контакт вручную' }));
  }
}

const busyNow = () => busy || Boolean(data?.operations.some((o) => o.status === 'pending'));

function render(force = false) {
  if (!data) return;
  renderStatus();
  const signature = JSON.stringify([data.messages, data.sent, data.operations, data.forwarding, data.limits, data.contacts,
    ui.group, ui.filter, ui.sort, ui.q, ui.contactQ, ui.selected, ui.selecting, [...ui.sel], busyNow()]);
  if (!force && signature === lastSignature) return;
  lastSignature = signature;
  indexContacts();
  let all = items();
  let allThreads = threads(all);
  if (autoRead(allThreads)) {
    all = items();
    allThreads = threads(all);
    renderStatus();
  }
  current = { items: all, threads: allThreads };
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
  if (confirm(`Переслать сообщение от ${displayName(it.addr)} на ${displayName(number)}? Будет отправлено SMS по тарифу оператора.`)) {
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
  const name = displayName(number);
  if (!confirm(`Отправить SMS ${name === number ? 'на ' + number : name + ' (' + number + ')'}?`)) return false;
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
  for (const v of ['messages', 'contacts', 'ussd', 'journal', 'settings']) $('view-' + v).hidden = v !== view;
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

function plural(n, one, few, many) {
  const m10 = n % 10;
  const m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  return m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
}

function resolveNumber(value) {
  const v = value.trim();
  if (isPhone(v)) return v.replace(/[\s()-]/g, '');
  const c = data.contacts.find((x) => x.name.toLowerCase() === v.toLowerCase());
  return c ? c.number : v;
}

function openCompose(number = threadNumber()) {
  $('compose-number').value = number;
  $('compose-dialog').showModal();
  ($('compose-number').value ? $('compose-text') : $('compose-number')).focus();
}

function openContact(contact, number = '') {
  ui.editContact = contact ? contact.id : null;
  $('contact-title').textContent = contact ? 'Контакт' : 'Новый контакт';
  $('contact-name').value = contact?.name || '';
  $('contact-number').value = contact?.number || number;
  $('contact-note').value = contact?.note || '';
  $('contact-delete').hidden = !contact;
  $('contact-dialog').showModal();
  $('contact-name').focus();
}

async function readTextFile(file) {
  const buffer = await file.arrayBuffer();
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(buffer);
  } catch {
    // Older Windows exports of CSV are in cp1251.
    return new TextDecoder('windows-1251').decode(buffer);
  }
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
    setRadio('theme-choice', next);
  });
  $('theme-choice').addEventListener('click', (e) => {
    const b = e.target.closest('[data-value]');
    if (!b) return;
    prefs.set('theme', b.dataset.value);
    applyTheme(b.dataset.value);
    setRadio('theme-choice', b.dataset.value);
  });

  const settingsView = $('view-settings');
  const settingsLinks = [...$('settings-nav').querySelectorAll('a')];
  for (const a of settingsLinks) {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      if (a.dataset.go) { setView(a.dataset.go); return; }
      document.querySelector(a.getAttribute('href')).scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }
  settingsView.addEventListener('scroll', () => {
    const top = settingsView.getBoundingClientRect().top;
    let active = settingsLinks[0].getAttribute('href');
    for (const s of settingsView.querySelectorAll('.set-section')) if (s.getBoundingClientRect().top - top < 140) active = '#' + s.id;
    if (settingsView.scrollTop + settingsView.clientHeight >= settingsView.scrollHeight - 4) active = '#set-look';
    for (const a of settingsLinks) a.classList.toggle('active', a.getAttribute('href') === active);
  });

  $('select-toggle').addEventListener('click', () => {
    if (ui.selecting) { exitSelect(); return; }
    enterSelect();
    render(true);
  });
  $('bulk-cancel').addEventListener('click', exitSelect);
  $('bulk-all').addEventListener('click', () => {
    const all = [...ui.selectable].every((k) => ui.sel.has(k));
    if (all) ui.sel.clear(); else for (const k of ui.selectable) ui.sel.add(k);
    render(true);
  });
  $('bulk-read').addEventListener('click', () => setRead(selectedMessages().flatMap((m) => m.ids), true));
  $('bulk-unread').addEventListener('click', () => setRead(selectedMessages().flatMap((m) => m.ids), false, true));
  $('bulk-forward').addEventListener('click', async () => {
    const msgs = selectedMessages();
    const number = data.forwarding.number;
    if (!number) { toast('Сначала укажите номер в Настройках → Пересылка SMS', true); return; }
    if (msgs.length > 20) { toast('За раз можно переслать не больше 20 сообщений', true); return; }
    const word = plural(msgs.length, 'сообщение', 'сообщения', 'сообщений');
    if (!confirm(`Переслать ${msgs.length} ${word} на ${displayName(number)}? Каждое уходит отдельным платным SMS, длинные — несколькими.`)) return;
    if (await action('forward-batch', { message_ids: msgs.map((m) => m.id) }, 'Пересылка поставлена в очередь')) exitSelect();
  });
  $('bulk-delete').addEventListener('click', async () => {
    const msgs = selectedMessages();
    const word = plural(msgs.length, 'сообщение', 'сообщения', 'сообщений');
    if (!confirm(`Удалить из архива ${msgs.length} ${word}? Восстановить их будет нельзя. Отправленные SMS останутся в журнале.`)) return;
    try {
      const r = await post('/api/delete', { ids: msgs.map((m) => m.id), confirm: true });
      toast(`Удалено: ${r.deleted}`);
      exitSelect();
      await refresh();
    } catch (err) {
      toast(err.message, true);
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && ui.selecting && !document.querySelector('dialog[open]')) exitSelect();
  });

  $('thread-contact').addEventListener('click', () => {
    const t = current.threads.find((x) => x.key === ui.selected);
    if (t) openContact(t.contact, t.contact ? '' : String(t.addr));
  });
  $('contacts-search').addEventListener('input', () => { ui.contactQ = $('contacts-search').value; render(true); });
  $('contact-add').addEventListener('click', () => openContact(null));
  $('contact-close').addEventListener('click', () => $('contact-dialog').close());
  $('contact-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      const saved = await post('/api/contacts/save', {
        id: ui.editContact, name: $('contact-name').value, number: $('contact-number').value, note: $('contact-note').value,
      });
      $('contact-dialog').close();
      toast(`Сохранено: ${saved.name}`);
      await refresh();
    } catch (err) {
      toast(err.message, true);
    }
  });
  $('contact-delete').addEventListener('click', async () => {
    if (!confirm(`Удалить контакт «${$('contact-name').value}»? Переписка останется, но будет показываться номер.`)) return;
    try {
      await post('/api/contacts/delete', { ids: [ui.editContact] });
      $('contact-dialog').close();
      toast('Контакт удалён');
      await refresh();
    } catch (err) {
      toast(err.message, true);
    }
  });
  $('contacts-import').addEventListener('click', () => $('contacts-file').click());
  $('contacts-file').addEventListener('change', async () => {
    const file = $('contacts-file').files[0];
    $('contacts-file').value = '';
    if (!file) return;
    if (file.size > 1.5 * 1024 * 1024) { toast('Файл больше 1,5 МБ', true); return; }
    const overwrite = data.contacts.length > 0
      && confirm('Если номер уже есть в контактах, заменить его имя на имя из файла?\nОК — заменить, Отмена — оставить прежние имена.');
    try {
      const r = await post('/api/contacts/import', { filename: file.name, content: await readTextFile(file), overwrite });
      toast(`Номеров в файле: ${r.found}. Добавлено: ${r.added}, обновлено: ${r.updated}, без изменений: ${r.skipped}`);
      await refresh();
    } catch (err) {
      toast(err.message, true);
    }
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
  $('compose-open').addEventListener('click', () => openCompose());
  $('compose-fab').addEventListener('click', () => openCompose());
  $('compose-close').addEventListener('click', () => dialog.close());
  $('compose-translit').addEventListener('click', () => { $('compose-text').value = translit($('compose-text').value); updateCompose(); });
  $('compose-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const number = resolveNumber($('compose-number').value);
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
