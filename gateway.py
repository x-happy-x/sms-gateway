"""Modem operations over RouterOS: archive, SIM cleanup, USSD, sending and forwarding."""
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime

from contacts import normalize_number, number_key
from pdu import EXT, GSM, decode, ussd
from store import period_bounds

GSM_BASIC = set(GSM) - {'\x1b'}
GSM_EXTENDED = set(EXT.values())
SMS_UNITS = 160
FORWARD_HOURLY_LIMIT = 20

TRANSLIT = dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
                    ['a', 'b', 'v', 'g', 'd', 'e', 'yo', 'zh', 'z', 'i', 'y', 'k', 'l', 'm', 'n', 'o', 'p', 'r', 's',
                     't', 'u', 'f', 'kh', 'ts', 'ch', 'sh', 'shch', '', 'y', '', 'e', 'yu', 'ya']))
PUNCTUATION = {'«': '"', '»': '"', '“': '"', '”': '"', '„': '"', '‘': "'", '’': "'", '—': '-', '–': '-',
               '…': '...', '№': 'No', '\u00a0': ' ', '\t': ' '}


def units(text):
    return sum(2 if ch in GSM_EXTENDED else 1 for ch in text)


def to_gsm7(text):
    """Transliterates Cyrillic and replaces everything RouterOS cannot send."""
    out = []
    for ch in text:
        low = ch.lower()
        if low in TRANSLIT:
            v = TRANSLIT[low]
            out.append(v[:1].upper() + v[1:] if ch != low and v else v)
        elif ch in PUNCTUATION:
            out.append(PUNCTUATION[ch])
        elif ch in GSM_BASIC or ch in GSM_EXTENDED:
            out.append(ch)
        else:
            out.append('?')
    return ''.join(out)


def split_units(text, limit):
    """Longest prefix of text that fits into limit GSM7 units, and the rest."""
    used = 0
    for i, ch in enumerate(text):
        used += 2 if ch in GSM_EXTENDED else 1
        if used > limit:
            return text[:i], text[i:]
    return text, ''


def forward_parts(message, max_parts):
    """GSM7 SMS texts that carry an incoming message, each within 160 units."""
    try:
        stamp = datetime.fromisoformat(message['timestamp']).strftime('%d.%m %H:%M')
    except (TypeError, ValueError, KeyError):
        stamp = ''
    body = to_gsm7(f"SMS ot {message['sender']} {stamp}:\n{message['text']}".strip())
    if units(body) <= SMS_UNITS:
        return [body]
    parts, rest = [], body
    while rest:
        # Reserve room for a "(n/m) " prefix; 10 units covers up to two-digit counters.
        head, rest = split_units(rest, SMS_UNITS - 10)
        parts.append(head)
    if len(parts) > max_parts:
        parts = parts[:max_parts]
        parts[-1] = split_units(parts[-1], SMS_UNITS - 13)[0] + '...'
    if len(parts) == 1:
        return parts
    return [f'({i}/{len(parts)}) {p}' for i, p in enumerate(parts, 1)]


def validate_number(number):
    if not isinstance(number, str) or not re.fullmatch(r'\+?\d{3,15}', number):
        raise ValueError('Номер: от 3 до 15 цифр, допустим + в начале')


def validate_sms(number, text):
    validate_number(number)
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Введите текст SMS')
    if any(ch not in GSM_BASIC | GSM_EXTENDED for ch in text):
        raise ValueError('RouterOS отправляет только GSM7. Используйте кнопку «В транслит» и проверьте текст перед отправкой.')
    count = units(text)
    if count > SMS_UNITS:
        raise ValueError('Максимум 160 единиц GSM7. Символы ^ { } [ ] ~ | \\ и € занимают по 2.')
    return count


def same_number(a, b):
    return bool(a) and bool(b) and re.sub(r'\D', '', a)[-10:] == re.sub(r'\D', '', b)[-10:]


class LimitReached(Exception):
    """The monthly SMS quota would be exceeded."""


class Gateway:
    def __init__(self, store, connect, poll_seconds=30):
        self.store = store
        self.connect = connect
        self.poll_seconds = poll_seconds
        self.lock = threading.Lock()
        self.state = {'error': None, 'last_sync': None, 'used': None, 'capacity': None, 'auto_deleted': 0}

    # --- archive ------------------------------------------------------------

    def read_pdus(self, r):
        if '+CMGF: 0' not in r.at('AT+CMGF?'):
            r.at('AT+CMGF=0')
        out = r.at('AT+CMGL=4')
        if not re.search(r'\bOK\b', out):
            raise RuntimeError('Модем вернул неполный список SMS')
        return [(int(i), raw.upper()) for i, raw in re.findall(r'\+CMGL:\s*(\d+),[^\r\n]+[\r\n]+([0-9A-Fa-f]+)', out)]

    def archive(self, r):
        items = self.read_pdus(r)
        for slot, raw in items:
            try:
                data = decode(raw)
            except Exception as e:
                data = {'sender': 'Неизвестный', 'text': 'Не удалось декодировать: ' + str(e), 'timestamp': '',
                        'concat': None, 'decode_error': True}
            self.store.add_sms(hashlib.sha256(raw.encode()).hexdigest(), raw, data)
        deleted = self.release_archived(r, items)
        m = re.search(r'\+CPMS:\s*"[^"]+",(\d+),(\d+)', r.at('AT+CPMS?'))
        self.state.update(last_sync=time.time(), error=None, auto_deleted=deleted,
                          used=int(m[1]) if m else len(items), capacity=int(m[2]) if m else None)
        return items

    def release_archived(self, r, items):
        deleted = 0
        for slot, raw in items:
            row = self.store.sms_by_hash(hashlib.sha256(raw.encode()).hexdigest())
            if not row or row['raw'] != raw or json.loads(row['data']).get('decode_error'):
                continue
            # Delete only after the archive transaction committed and the slot still matches.
            current = r.at('AT+CMGR=' + str(slot))
            if raw not in {line.strip().upper() for line in current.splitlines()}:
                continue
            if not re.search(r'\bOK\b', r.at('AT+CMGD=' + str(slot))):
                raise RuntimeError('Модем не подтвердил очистку ячейки SIM')
            deleted += 1
        return deleted

    def clear_archived(self, r):
        self.archive(r)
        return {'text': f"Освобождено ячеек SIM: {self.state.get('auto_deleted', 0)}. SMS сохранены в архиве."}

    # --- USSD and sending ---------------------------------------------------

    @staticmethod
    def _logs(r):
        return [x for x in r.call('/log/print', query=['?buffer=smsgw'])
                if any(k in x.get('message', '') for k in ('+CUSD:', '+CMGS:', '+CMS ERROR:'))]

    def do_ussd(self, r, code):
        key = lambda x: (x.get('.id'), x.get('time'), x.get('message'))
        seen = {key(x) for x in self._logs(r)}
        out = r.at('AT+CUSD=1,"' + code + '",15', True)
        deadline = time.monotonic() + 120
        while True:
            if '+CUSD:' in out:
                return ussd(out)
            for row in self._logs(r):
                if key(row) not in seen and '+CUSD:' in row.get('message', ''):
                    return ussd(row['message'])
            if time.monotonic() > deadline:
                raise TimeoutError('Ответ USSD не получен за 120 секунд')
            time.sleep(1)

    def limit_status(self, now=None):
        settings = self.store.limit_settings()
        start, next_reset = period_bounds(settings['reset_day'], now)
        used = self.store.sent_count(start)
        monthly = settings['monthly']
        return {**settings, 'used': used, 'remaining': max(0, monthly - used) if monthly else None,
                'period_start': start, 'next_reset': next_reset}

    def ensure_quota(self, count):
        status = self.limit_status()
        if status['monthly'] and status['used'] + count > status['monthly']:
            reset = time.strftime('%d.%m.%Y', time.localtime(status['next_reset']))
            raise LimitReached(f"Месячный лимит SMS: отправлено {status['used']} из {status['monthly']}, "
                               f"нужно ещё {count}. Лимит обновится {reset}.")

    def send_sms(self, r, number, text, operation=None):
        validate_sms(number, text)
        self.ensure_quota(1)
        r.call('/tool/sms/send', **{'port': 'lte1', 'phone-number': number, 'message': text, 'status-report-request': 'no'})
        self.store.log_sent(1, operation)
        return {'text': 'RouterOS подтвердил отправку SMS. Это не отчёт о доставке.'}

    def send_parts(self, r, number, parts, operation=None):
        self.ensure_quota(len(parts))
        sent = 0
        try:
            for part in parts:
                self.send_sms(r, number, part, operation)
                sent += 1
        except Exception as e:
            raise RuntimeError(f'Отправлено частей: {sent} из {len(parts)}. {e}') from e
        return {'text': f'RouterOS подтвердил отправку {sent} SMS на {number}. Это не отчёт о доставке.'}

    # --- operations ---------------------------------------------------------

    def run(self, oid, kind, payload):
        """Executes a registered operation in the background thread."""
        try:
            with self.lock:
                r = self.connect()
                try:
                    if kind == 'ussd':
                        result = self.do_ussd(r, payload['code'])
                    elif kind == 'send':
                        result = self.send_sms(r, payload['number'], payload['text'], oid)
                    elif kind == 'clear':
                        result = self.clear_archived(r)
                    elif kind in ('forward', 'forward-test'):
                        result = self.send_parts(r, payload['number'], payload['parts'], oid)
                    elif kind == 'forward-batch':
                        result = self.forward_batch(r, payload, oid)
                    else:
                        self.archive(r)
                        result = {'text': 'SMS обновлены'}
                finally:
                    r.close()
            status = 'done'
        except Exception as e:
            status, result = 'error', {'text': str(e)}
        self.store.finish_operation(oid, status, result)
        if kind == 'forward' and payload.get('message_id'):
            self.store.record_forward(payload['message_id'], oid, status)

    def forward_batch(self, r, payload, oid):
        """Forwards several messages in one operation; stops at the first failure."""
        items = payload['items']
        self.ensure_quota(sum(len(item['parts']) for item in items))
        done = 0
        for item in items:
            self.store.record_forward(item['message_id'], oid, 'pending')
            try:
                self.send_parts(r, payload['number'], item['parts'], oid)
            except Exception as e:
                self.store.record_forward(item['message_id'], oid, 'error')
                raise RuntimeError(f'Переслано сообщений: {done} из {len(items)}. {e}') from e
            self.store.record_forward(item['message_id'], oid, 'done')
            done += 1
        return {'text': f'Переслано сообщений: {done} на {payload["number"]}. Это подтверждение RouterOS, а не доставки.'}

    def forward_request(self, message, settings):
        number = settings['number']
        validate_number(number)
        sender = message['sender']
        if normalize_number(sender):
            name = next((c['name'] for c in self.store.contacts() if number_key(c['number']) == number_key(sender)), None)
            if name:
                sender = f'{name} ({sender})'
        return {'message_id': message['id'], 'number': number, 'sender': sender,
                'parts': forward_parts(dict(message, sender=sender), int(settings.get('max_parts') or 3))}

    def process_forwards(self, r):
        """Forwards new messages when enabled; never retries a failed send automatically."""
        settings = self.store.forward_settings()
        self.state.pop('forward_note', None)
        if not settings['enabled'] or not settings['number']:
            return
        for message in self.store.forward_candidates(settings):
            if same_number(message['sender'], settings['number']):
                self.store.record_forward(message['id'], None, 'skipped')
                continue
            if self.store.sent_since(time.time() - 3600) >= FORWARD_HOURLY_LIMIT:
                self.state['forward_note'] = f'Достигнут лимит {FORWARD_HOURLY_LIMIT} пересылок в час, остальные ждут.'
                return
            request = dict(self.forward_request(message, settings), auto=True)
            try:
                self.ensure_quota(len(request['parts']))
            except LimitReached as e:
                # Not queued: after the reset a backlog of stale messages would go out at once.
                self.store.record_forward(message['id'], None, 'limit')
                self.state['forward_note'] = str(e)
                continue
            oid = 'fwd-' + uuid.uuid4().hex
            self.store.start_operation(oid, 'forward', request, exclusive=False)
            self.store.record_forward(message['id'], oid, 'pending')
            try:
                result, status = self.send_parts(r, request['number'], request['parts'], oid), 'done'
            except Exception as e:
                result, status = {'text': str(e)}, 'error'
            self.store.finish_operation(oid, status, result)
            self.store.record_forward(message['id'], oid, status)
            if status == 'error':
                # The connection may be broken; the rest waits for the next cycle.
                return

    def sync(self):
        with self.lock:
            r = self.connect()
            try:
                self.archive(r)
                self.process_forwards(r)
            finally:
                r.close()

    def poll(self):
        while True:
            try:
                self.sync()
            except Exception as e:
                self.state['error'] = str(e)
                logging.warning('SMS sync: %s', e)
            time.sleep(self.poll_seconds)
