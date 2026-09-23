#!/opt/bin/python3
"""SMS Gateway: web UI and token API for the MikroTik LTE modem, running on Entware."""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from contacts import normalize_number, number_key, parse_contacts
from gateway import Gateway, LimitReached, validate_number, validate_sms, forward_parts
from store import Busy, Store

BASE = Path(__file__).resolve().parent
HOME = Path(os.environ.get('SMSGW_HOME') or BASE)
CONFIG = HOME / 'config.json'
STATIC = BASE / 'static'
STATIC_TYPES = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
                '.js': 'text/javascript; charset=utf-8', '.svg': 'image/svg+xml'}
MAX_BODY = 16384
MAX_IMPORT = 2 * 1024 * 1024
OPERATION_ID = re.compile(r'[a-zA-Z0-9-]{16,80}')
UI_KINDS = ('ussd', 'send', 'sync', 'clear', 'forward', 'forward-test', 'forward-batch')
SENT_KINDS = ('send', 'forward', 'forward-test', 'forward-batch')
MAX_BATCH = 20
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def load_config():
    return json.loads(CONFIG.read_text())


def save_config(cfg):
    tmp = CONFIG.with_suffix('.json.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, CONFIG)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def public_message(m, names=None):
    return {'id': m['id'], 'ids': m['ids'], 'sender': m['sender'],
            'sender_name': (names or {}).get(number_key(m['sender'])) if normalize_number(m['sender']) else None,
            'text': m['text'], 'timestamp': m['timestamp'],
            'received_at': m['added'], 'read': m['read'], 'parts': m.get('parts'), 'complete': m['complete'],
            'decode_error': bool(m.get('decode_error')), 'forward': m['forward']}


class HttpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class App:
    def __init__(self, cfg, store, gateway):
        self.cfg = cfg
        self.store = store
        self.gateway = gateway
        self.csrf = secrets.token_urlsafe(32)

    # --- auth ---------------------------------------------------------------

    def api_client(self, header):
        match = re.fullmatch(r'Bearer\s+(\S+)', header or '')
        if not match:
            raise HttpError(401, 'Нужен заголовок Authorization: Bearer <token>')
        digest = token_hash(match[1])
        for item in self.cfg.get('api_tokens', []):
            if hmac.compare_digest(digest, item.get('sha256', '')):
                return item.get('name', 'api')
        raise HttpError(401, 'Неверный токен')

    # --- operations ---------------------------------------------------------

    def build_request(self, kind, p):
        if kind == 'ussd':
            if not isinstance(p.get('code'), str) or not re.fullmatch(r'[0-9*#]{1,40}', p['code']):
                raise ValueError('Допустимы цифры, * и #')
            return {'code': p['code']}
        if kind == 'send':
            validate_sms(p.get('number', ''), p.get('text', ''))
            return {'number': p['number'], 'text': p['text']}
        if kind == 'clear':
            if p.get('confirm') is not True:
                raise ValueError('Подтвердите очистку SIM')
            return {'confirm': True}
        if kind == 'forward':
            settings = self.store.forward_settings()
            message = next((m for m in self.store.messages() if m['id'] == p.get('message_id')), None)
            if message is None:
                raise ValueError('Сообщение не найдено')
            if not settings['number']:
                raise ValueError('Сначала укажите номер для пересылки')
            return self.gateway.forward_request(message, settings)
        if kind == 'forward-batch':
            ids = p.get('message_ids')
            if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
                raise ValueError('message_ids: непустой список чисел')
            if len(ids) > MAX_BATCH:
                raise ValueError(f'За раз можно переслать не больше {MAX_BATCH} сообщений')
            settings = self.store.forward_settings()
            if not settings['number']:
                raise ValueError('Сначала укажите номер для пересылки')
            by_id = {m['id']: m for m in self.store.messages()}
            missing = [i for i in ids if i not in by_id]
            if missing:
                raise ValueError(f'Сообщения не найдены: {missing}')
            items = [self.gateway.forward_request(by_id[i], settings) for i in sorted(set(ids), key=lambda i: by_id[i]['added'])]
            return {'number': settings['number'], 'count': len(items),
                    'items': [{'message_id': it['message_id'], 'sender': it['sender'], 'parts': it['parts']} for it in items]}
        if kind == 'forward-test':
            settings = self.store.forward_settings()
            number = p.get('number') or settings['number']
            validate_number(number)
            sample = {'sender': 'SMS Gateway', 'timestamp': datetime.now(timezone.utc).astimezone().isoformat(),
                      'text': 'Проверка пересылки. Если вы это читаете, пересылка работает.'}
            return {'number': number, 'test': True, 'parts': forward_parts(sample, int(settings['max_parts']))}
        return {}

    def submit(self, kind, payload, via):
        oid = payload.get('id') or str(uuid.uuid4())
        if not isinstance(oid, str) or not OPERATION_ID.fullmatch(oid):
            raise ValueError('id операции: 16–80 символов a-z, A-Z, 0-9 и -')
        request = dict(self.build_request(kind, payload), via=via)
        try:
            if kind == 'send':
                self.gateway.ensure_quota(1)
            elif kind in ('forward', 'forward-test'):
                self.gateway.ensure_quota(len(request['parts']))
            elif kind == 'forward-batch':
                self.gateway.ensure_quota(sum(len(item['parts']) for item in request['items']))
            created = self.store.start_operation(oid, kind, request)
        except LimitReached as e:
            raise HttpError(429, str(e))
        except Busy as e:
            raise HttpError(409, str(e))
        if created:
            threading.Thread(target=self.gateway.run, args=(oid, kind, request), daemon=True).start()
        return oid, created

    def save_forwarding(self, p):
        settings = self.store.forward_settings()
        enabled = p.get('enabled') is True
        number = str(p.get('number', settings['number'])).strip()
        max_parts = p.get('max_parts', settings['max_parts'])
        if number or enabled:
            validate_number(number)
        if not isinstance(max_parts, int) or not 1 <= max_parts <= 5:
            raise ValueError('Частей на одно сообщение: от 1 до 5')
        # Only messages received after enabling are forwarded, never the backlog.
        since = settings['since'] if settings['enabled'] and enabled else (time.time() if enabled else None)
        settings.update(enabled=enabled, number=number, max_parts=max_parts, since=since)
        self.store.save_forward_settings(settings)
        return settings

    def save_limits(self, p):
        monthly, reset_day = p.get('monthly'), p.get('reset_day')
        if not isinstance(monthly, int) or not 0 <= monthly <= 100000:
            raise ValueError('Лимит: целое число от 0 (без лимита) до 100000')
        if not isinstance(reset_day, int) or not 1 <= reset_day <= 28:
            raise ValueError('День обновления лимита: от 1 до 28')
        self.store.save_limit_settings({'monthly': monthly, 'reset_day': reset_day})
        return self.gateway.limit_status()

    # --- queries ------------------------------------------------------------

    def contact_names(self):
        return {number_key(c['number']): c['name'] for c in self.store.contacts()}

    def save_contact(self, p):
        name = str(p.get('name') or '').strip()
        number = normalize_number(p.get('number'))
        note = str(p.get('note') or '').strip()
        cid = p.get('id')
        if not name or len(name) > 120:
            raise ValueError('Имя: от 1 до 120 символов')
        if not number:
            raise ValueError('Номер: от 3 до 15 цифр, допустим + в начале')
        if len(note) > 500:
            raise ValueError('Заметка: до 500 символов')
        if cid is not None and not isinstance(cid, int):
            raise ValueError('id контакта должен быть числом')
        cid = self.store.save_contact(cid, name, number, note, number_key(number))
        return {'id': cid, 'name': name, 'number': number, 'note': note}

    def import_contacts(self, p):
        content = p.get('content')
        if not isinstance(content, str) or not content.strip():
            raise ValueError('Файл пустой')
        parsed = parse_contacts(str(p.get('filename') or ''), content)
        if not parsed:
            raise ValueError('В файле не найдено ни одного номера. Поддерживаются vCard (.vcf) и CSV.')
        unique = {}
        for item in parsed:
            unique.setdefault(number_key(item['number']), (item['name'][:120], item['number'], number_key(item['number'])))
        result = self.store.import_contacts(unique.values(), overwrite=p.get('overwrite') is True)
        return {**result, 'found': len(unique)}

    def ui_state(self):
        return {
            'state': dict(self.gateway.state),
            'messages': [public_message(m) for m in self.store.messages()],
            'contacts': self.store.contacts(),
            'sent': self.store.operations(300, kinds=SENT_KINDS),
            'operations': self.store.operations(40),
            'forwarding': self.store.forward_settings(),
            'limits': self.gateway.limit_status(),
            'api_enabled': bool(self.cfg.get('api_tokens')),
            'csrf': self.csrf,
        }

    def list_messages(self, query):
        def one(name, default=None):
            return query.get(name, [default])[0]
        names = self.contact_names()
        messages = [public_message(m, names) for m in self.store.messages()]
        unread = one('unread')
        if unread in ('1', 'true'):
            messages = [m for m in messages if not m['read']]
        elif unread in ('0', 'false'):
            messages = [m for m in messages if m['read']]
        if one('sender'):
            messages = [m for m in messages if m['sender'] == one('sender')]
        if one('since'):
            since = one('since')
            try:
                since = float(since) if re.fullmatch(r'\d+(\.\d+)?', since) else datetime.fromisoformat(since).timestamp()
            except ValueError:
                raise ValueError('since: Unix-время или ISO 8601')
            messages = [m for m in messages if m['received_at'] >= since]
        if one('order', 'desc') == 'asc':
            messages.reverse()
        try:
            limit, offset = int(one('limit', 50)), int(one('offset', 0))
        except ValueError:
            raise ValueError('limit и offset должны быть числами')
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError('limit: 1–500, offset: от 0')
        return {'total': len(messages), 'messages': messages[offset:offset + limit]}

    def find_message(self, mid):
        names = self.contact_names()
        return next((public_message(m, names) for m in self.store.messages() if mid in m['ids']), None)


class Handler(BaseHTTPRequestHandler):
    app = None
    server_version = 'SMSGateway'
    sys_version = ''

    def log_message(self, *args):
        pass

    def reply(self, code, obj, ctype='application/json; charset=utf-8'):
        body = json.dumps(obj, ensure_ascii=False).encode() if ctype.startswith('application/json') else obj
        self.send_response(code)
        for name, value in (('Content-Type', ctype), ('Content-Length', str(len(body))), ('Cache-Control', 'no-store'),
                            ('X-Content-Type-Options', 'nosniff'), ('X-Frame-Options', 'DENY'),
                            ('Referrer-Policy', 'no-referrer'), ('Content-Security-Policy', CSP)):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def body(self, limit=MAX_BODY):
        n = int(self.headers.get('Content-Length', '0'))
        if not 0 < n <= limit:
            raise ValueError('Недопустимый размер запроса')
        if not self.headers.get('Content-Type', '').startswith('application/json'):
            raise ValueError('Ожидается JSON')
        data = json.loads(self.rfile.read(n))
        if not isinstance(data, dict):
            raise ValueError('Ожидается JSON-объект')
        return data

    def handle_errors(self, action):
        try:
            action()
        except HttpError as e:
            self.reply(e.code, {'error': str(e)})
        except (ValueError, TypeError, KeyError) as e:
            self.reply(400, {'error': str(e)})
        except Exception:
            logging.exception('HTTP %s %s', self.command, self.path)
            self.reply(500, {'error': 'Внутренняя ошибка сервиса'})

    def do_GET(self):
        self.handle_errors(self.get)

    def do_POST(self):
        self.handle_errors(self.post)

    def get(self):
        url = urlsplit(self.path)
        path, app = url.path, self.app
        if path == '/':
            return self.static('index.html')
        if path.startswith('/static/'):
            return self.static(path.removeprefix('/static/'))
        if path == '/api/health':
            return self.reply(200, {'ok': True})
        if path == '/api/state':
            return self.reply(200, app.ui_state())
        if path.startswith('/api/v1/'):
            client = app.api_client(self.headers.get('Authorization'))
            if path == '/api/v1/messages':
                return self.reply(200, app.list_messages(parse_qs(url.query)))
            if m := re.fullmatch(r'/api/v1/messages/(\d+)', path):
                message = app.find_message(int(m[1]))
                return self.reply(200, message) if message else self.reply(404, {'error': 'Сообщение не найдено'})
            if path == '/api/v1/contacts':
                return self.reply(200, {'contacts': app.store.contacts()})
            if path == '/api/v1/limits':
                return self.reply(200, app.gateway.limit_status())
            if m := re.fullmatch(r'/api/v1/operations/([a-zA-Z0-9-]{16,80})', path):
                op = app.store.operation(m[1])
                return self.reply(200, op) if op else self.reply(404, {'error': 'Операция не найдена'})
            logging.info('API %s: unknown %s', client, path)
        self.reply(404, {'error': 'Не найдено'})

    def post(self):
        path, app = urlsplit(self.path).path, self.app
        if path.startswith('/api/v1/'):
            client = app.api_client(self.headers.get('Authorization'))
            p = self.body()
            if path == '/api/v1/send':
                oid, created = app.submit('send', p, 'api:' + client)
                logging.info('API %s: send %s', client, oid)
                return self.reply(202 if created else 200, {'id': oid, 'status_url': f'/api/v1/operations/{oid}'})
            if path == '/api/v1/messages/read':
                ids = p.get('ids')
                if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                    raise ValueError('ids: список чисел')
                return self.reply(200, {'updated': app.store.mark_read(ids, p.get('read', True) is not False)})
            return self.reply(404, {'error': 'Не найдено'})
        if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), app.csrf):
            return self.reply(403, {'error': 'Обновите страницу'})
        kind = path.removeprefix('/api/')
        p = self.body(MAX_IMPORT if kind == 'contacts/import' else MAX_BODY)
        if kind in UI_KINDS:
            oid, created = app.submit(kind, p, 'ui')
            return self.reply(202 if created else 200, {'id': oid})
        if kind == 'read':
            ids = p.get('ids')
            if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                raise ValueError('ids: список чисел')
            return self.reply(200, {'updated': app.store.mark_read(ids, p.get('read', True) is not False)})
        if kind == 'contacts/save':
            return self.reply(200, app.save_contact(p))
        if kind == 'contacts/delete':
            ids = p.get('ids')
            if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
                raise ValueError('ids: непустой список чисел')
            return self.reply(200, {'deleted': app.store.delete_contacts(ids)})
        if kind == 'contacts/import':
            return self.reply(200, app.import_contacts(p))
        if kind == 'delete':
            ids = p.get('ids')
            if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
                raise ValueError('ids: непустой список чисел')
            if p.get('confirm') is not True:
                raise ValueError('Подтвердите удаление')
            return self.reply(200, {'deleted': app.store.delete_messages(ids)})
        if kind == 'forwarding':
            return self.reply(200, app.save_forwarding(p))
        if kind == 'limits':
            return self.reply(200, app.save_limits(p))
        self.reply(404, {'error': 'Не найдено'})

    def static(self, name):
        path = (STATIC / name).resolve()
        if not re.fullmatch(r'[\w.-]+', name) or path.parent != STATIC.resolve() or not path.is_file() \
                or path.suffix not in STATIC_TYPES:
            return self.reply(404, {'error': 'Не найдено'})
        self.reply(200, path.read_bytes(), STATIC_TYPES[path.suffix])


def token_command(args):
    cfg = load_config()
    tokens = cfg.setdefault('api_tokens', [])
    if args[:1] == ['add'] and len(args) == 2:
        name = args[1]
        if any(t['name'] == name for t in tokens):
            sys.exit(f'Токен {name} уже есть')
        token = secrets.token_urlsafe(32)
        tokens.append({'name': name, 'sha256': token_hash(token), 'created': int(time.time())})
        save_config(cfg)
        print(token)
        print('Сохраните токен: повторно его показать нельзя. Перезапустите сервис, чтобы он начал действовать.',
              file=sys.stderr)
    elif args[:1] == ['remove'] and len(args) == 2:
        cfg['api_tokens'] = [t for t in tokens if t['name'] != args[1]]
        save_config(cfg)
    elif args == ['list']:
        for t in tokens:
            print(t['name'], time.strftime('%Y-%m-%d %H:%M', time.localtime(t.get('created', 0))))
    else:
        sys.exit('Использование: server.py token add NAME | token remove NAME | token list')


def main():
    if sys.argv[1:2] == ['token']:
        return token_command(sys.argv[2:])
    from routeros import RouterOS
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    os.umask(0o077)
    cfg = load_config()
    store = Store(HOME / 'archive.sqlite')
    store.initialize()
    gateway = Gateway(store, lambda: RouterOS(cfg['router']), cfg.get('poll_seconds', 30))
    Handler.app = App(cfg, store, gateway)
    threading.Thread(target=gateway.poll, daemon=True).start()
    server = ThreadingHTTPServer((cfg.get('listen', '192.168.1.1'), cfg.get('port', 8099)), Handler)
    logging.info('SMS Gateway listening on %s:%s', *server.server_address)
    server.serve_forever()


if __name__ == '__main__':
    main()
