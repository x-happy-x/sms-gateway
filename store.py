"""SQLite archive: received SMS, read state, operations, settings and forwards."""
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime

# A concatenated SMS that is still missing parts is forwarded anyway after this delay.
INCOMPLETE_GRACE = 600

DEFAULT_FORWARD = {'enabled': False, 'number': '', 'max_parts': 3, 'since': None}
DEFAULT_LIMITS = {'monthly': 0, 'reset_day': 1}


def period_bounds(reset_day, now=None):
    """Start of the current monthly quota period and of the next one, in local time."""
    t = time.localtime(now or time.time())
    year, month = t.tm_year, t.tm_mon
    if t.tm_mday < reset_day:
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    start = time.mktime((year, month, reset_day, 0, 0, 0, 0, 0, -1))
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return start, time.mktime((ny, nm, reset_day, 0, 0, 0, 0, 0, -1))


class Busy(Exception):
    """Another operation is still running."""


class Store:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def tx(self, immediate=False):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            if immediate:
                c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def initialize(self):
        with self.tx() as c:
            c.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sms (id INTEGER PRIMARY KEY, hash TEXT UNIQUE, raw TEXT NOT NULL, data TEXT NOT NULL, added REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, kind TEXT, request TEXT, status TEXT, result TEXT, added REAL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS forwards (message_id INTEGER PRIMARY KEY, operation TEXT, status TEXT NOT NULL, added REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS contacts (id INTEGER PRIMARY KEY, name TEXT NOT NULL, number TEXT NOT NULL, key TEXT NOT NULL UNIQUE, note TEXT NOT NULL DEFAULT '', updated REAL NOT NULL);
            UPDATE operations SET status='unknown',result='{"text":"Сервис перезапущен во время операции. Проверьте результат перед повтором."}' WHERE status='pending';''')
            columns = {row['name'] for row in c.execute('PRAGMA table_info(sms)')}
            if 'read_at' not in columns:
                c.execute('ALTER TABLE sms ADD COLUMN read_at REAL')
                # Everything archived before read tracking existed was already shown in the old UI.
                c.execute('UPDATE sms SET read_at=added')
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sent_log'").fetchone():
                c.execute('CREATE TABLE sent_log (id INTEGER PRIMARY KEY, at REAL NOT NULL, count INTEGER NOT NULL, operation TEXT)')
                # Seed the quota with SMS confirmed before per-SMS accounting existed.
                for op in c.execute("SELECT id,kind,request,added FROM operations WHERE status='done' AND kind IN ('send','forward','forward-test')"):
                    count = 1 if op['kind'] == 'send' else len(json.loads(op['request'] or '{}').get('parts') or [])
                    if count:
                        c.execute('INSERT INTO sent_log(at,count,operation) VALUES(?,?,?)', (op['added'], count, op['id']))

    # --- messages -----------------------------------------------------------

    def add_sms(self, digest, raw, data):
        with self.tx() as c:
            c.execute('INSERT OR IGNORE INTO sms(hash,raw,data,added) VALUES(?,?,?,?)',
                      (digest, raw, json.dumps(data, ensure_ascii=False), time.time()))

    def sms_by_hash(self, digest):
        with self.tx() as c:
            return c.execute('SELECT raw,data FROM sms WHERE hash=?', (digest,)).fetchone()

    def messages(self):
        """Logical messages (concatenated parts joined), newest first."""
        with self.tx() as c:
            rows = c.execute('SELECT id,data,added,read_at FROM sms ORDER BY added,id').fetchall()
            forwards = {row['message_id']: dict(row) for row in c.execute('SELECT * FROM forwards')}
        out, groups = [], {}
        for row in rows:
            d = json.loads(row['data'])
            d.update(id=row['id'], ids=[row['id']], added=row['added'], unread=[row['id']] if row['read_at'] is None else [])
            con = d.get('concat')
            if not con:
                out.append(d)
                continue
            ref, total, part = con
            key = (d['sender'], ref, total)
            try:
                stamp = datetime.fromisoformat(d['timestamp']).timestamp()
            except (TypeError, ValueError):
                stamp = d['added']
            # A reused concatenation reference starts a new group after 24h or a repeated part.
            group = next((g for g in reversed(groups.setdefault(key, []))
                          if part not in g['parts'] and abs(stamp - g['stamp']) < 86400), None)
            if group is None:
                group = {'base': dict(d, ids=[], unread=[]), 'parts': {}, 'stamp': stamp}
                groups[key].append(group)
            group['parts'][part] = d['text']
            group['base']['ids'].append(row['id'])
            group['base']['unread'] += d['unread']
        for (_, _, total), gs in groups.items():
            for g in gs:
                d = g['base']
                d['id'] = min(d['ids'])
                d['text'] = ''.join(g['parts'].get(i, '\n[Ожидается часть %s]\n' % i) for i in range(1, total + 1))
                d['parts'] = f"{len(g['parts'])}/{total}"
                d['complete'] = len(g['parts']) == total
                out.append(d)
        for d in out:
            d.setdefault('complete', True)
            d['read'] = not d.pop('unread')
            d['forward'] = forwards.get(d['id'])
            d.pop('concat', None)
        return sorted(out, key=lambda m: (m['timestamp'] or '', m['added']), reverse=True)

    def mark_read(self, ids, read=True):
        """Marks whole logical messages; any id of a message selects all its parts."""
        wanted = {int(i) for i in ids}
        rows = [i for m in self.messages() if wanted & set(m['ids']) for i in m['ids']]
        if not rows:
            return 0
        marks = ','.join('?' * len(rows))
        with self.tx() as c:
            if read:
                c.execute(f'UPDATE sms SET read_at=COALESCE(read_at,?) WHERE id IN ({marks})', (time.time(), *rows))
            else:
                c.execute(f'UPDATE sms SET read_at=NULL WHERE id IN ({marks})', rows)
        return len(rows)

    def delete_messages(self, ids):
        """Deletes whole logical messages from the archive; returns how many were deleted."""
        wanted = {int(i) for i in ids}
        chosen = [m for m in self.messages() if wanted & set(m['ids'])]
        rows = [i for m in chosen for i in m['ids']]
        if not rows:
            return 0
        with self.tx() as c:
            c.execute(f"DELETE FROM sms WHERE id IN ({','.join('?' * len(rows))})", rows)
            c.execute(f"DELETE FROM forwards WHERE message_id IN ({','.join('?' * len(chosen))})", [m['id'] for m in chosen])
        return len(chosen)

    # --- operations ---------------------------------------------------------

    def start_operation(self, oid, kind, request, exclusive=True):
        """Registers an operation; returns False when the id already exists (idempotent retry)."""
        with self.tx(immediate=True) as c:
            if c.execute('SELECT 1 FROM operations WHERE id=?', (oid,)).fetchone():
                return False
            if exclusive and c.execute("SELECT 1 FROM operations WHERE status='pending'").fetchone():
                raise Busy('Дождитесь завершения текущей операции')
            c.execute('INSERT INTO operations VALUES(?,?,?,?,?,?)',
                      (oid, kind, json.dumps(request, ensure_ascii=False), 'pending', None, time.time()))
        return True

    def finish_operation(self, oid, status, result):
        with self.tx() as c:
            c.execute('UPDATE operations SET status=?,result=? WHERE id=?',
                      (status, json.dumps(result, ensure_ascii=False), oid))

    @staticmethod
    def _operation(row):
        op = dict(row)
        op['request'] = json.loads(op['request'] or '{}')
        try:
            op['result'] = json.loads(op['result']) if op['result'] else None
        except ValueError:
            op['result'] = {'text': op['result']}
        return op

    def operation(self, oid):
        with self.tx() as c:
            row = c.execute('SELECT * FROM operations WHERE id=?', (oid,)).fetchone()
        return self._operation(row) if row else None

    def operations(self, limit=30, kinds=None):
        query, args = 'SELECT * FROM operations', []
        if kinds:
            query += ' WHERE kind IN (%s)' % ','.join('?' * len(kinds))
            args += list(kinds)
        with self.tx() as c:
            rows = c.execute(query + ' ORDER BY added DESC LIMIT ?', (*args, limit)).fetchall()
        return [self._operation(r) for r in rows]

    def sent_since(self, since, kinds=('forward',)):
        with self.tx() as c:
            return c.execute('SELECT COUNT(*) FROM operations WHERE added>=? AND kind IN (%s)' % ','.join('?' * len(kinds)),
                             (since, *kinds)).fetchone()[0]

    # --- contacts -----------------------------------------------------------

    def contacts(self):
        with self.tx() as c:
            return [dict(r) for r in c.execute('SELECT id,name,number,note FROM contacts ORDER BY name COLLATE NOCASE, number')]

    def save_contact(self, cid, name, number, note, key):
        with self.tx(immediate=True) as c:
            other = c.execute('SELECT id,name FROM contacts WHERE key=?', (key,)).fetchone()
            if other and other['id'] != cid:
                raise ValueError(f"Номер уже записан у контакта «{other['name']}»")
            if cid is None:
                cur = c.execute('INSERT INTO contacts(name,number,key,note,updated) VALUES(?,?,?,?,?)', (name, number, key, note, time.time()))
                cid = cur.lastrowid
            elif c.execute('UPDATE contacts SET name=?,number=?,key=?,note=?,updated=? WHERE id=?',
                           (name, number, key, note, time.time(), cid)).rowcount == 0:
                raise ValueError('Контакт не найден')
        return cid

    def delete_contacts(self, ids):
        ids = [int(i) for i in ids]
        with self.tx() as c:
            return c.execute(f"DELETE FROM contacts WHERE id IN ({','.join('?' * len(ids))})", ids).rowcount

    def import_contacts(self, items, overwrite=False):
        """Adds (name, number, key) items; existing numbers keep their name unless overwrite."""
        added = updated = skipped = 0
        with self.tx(immediate=True) as c:
            for name, number, key in items:
                row = c.execute('SELECT id,name FROM contacts WHERE key=?', (key,)).fetchone()
                if row is None:
                    c.execute('INSERT INTO contacts(name,number,key,note,updated) VALUES(?,?,?,?,?)', (name, number, key, '', time.time()))
                    added += 1
                elif overwrite and row['name'] != name:
                    c.execute('UPDATE contacts SET name=?,number=?,updated=? WHERE id=?', (name, number, time.time(), row['id']))
                    updated += 1
                else:
                    skipped += 1
        return {'added': added, 'updated': updated, 'skipped': skipped}

    # --- settings and forwards ----------------------------------------------

    def forward_settings(self):
        with self.tx() as c:
            row = c.execute("SELECT value FROM settings WHERE key='forward'").fetchone()
        return {**DEFAULT_FORWARD, **(json.loads(row['value']) if row else {})}

    def save_forward_settings(self, settings):
        with self.tx() as c:
            c.execute("INSERT INTO settings(key,value) VALUES('forward',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (json.dumps(settings, ensure_ascii=False),))

    def limit_settings(self):
        with self.tx() as c:
            row = c.execute("SELECT value FROM settings WHERE key='limits'").fetchone()
        return {**DEFAULT_LIMITS, **(json.loads(row['value']) if row else {})}

    def save_limit_settings(self, settings):
        with self.tx() as c:
            c.execute("INSERT INTO settings(key,value) VALUES('limits',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (json.dumps(settings),))

    def log_sent(self, count, operation=None):
        with self.tx() as c:
            c.execute('INSERT INTO sent_log(at,count,operation) VALUES(?,?,?)', (time.time(), count, operation))

    def sent_count(self, since):
        with self.tx() as c:
            return c.execute('SELECT COALESCE(SUM(count),0) FROM sent_log WHERE at>=?', (since,)).fetchone()[0]

    def record_forward(self, message_id, operation, status):
        with self.tx() as c:
            c.execute('INSERT INTO forwards(message_id,operation,status,added) VALUES(?,?,?,?) '
                      'ON CONFLICT(message_id) DO UPDATE SET operation=excluded.operation,status=excluded.status,added=excluded.added',
                      (message_id, operation, status, time.time()))

    def forward_candidates(self, settings, now=None):
        """Messages received after forwarding was enabled that were never forwarded."""
        now = now or time.time()
        since = settings.get('since') or now
        out = []
        for m in reversed(self.messages()):
            if m['forward'] or m['added'] < since or m.get('decode_error'):
                continue
            if not m['complete'] and now - m['added'] < INCOMPLETE_GRACE:
                continue
            out.append(m)
        return out
