import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gateway  # noqa: E402
import server  # noqa: E402
from gateway import Gateway, forward_parts, to_gsm7, units  # noqa: E402
from store import Store, period_bounds  # noqa: E402


class FakeRouter:
    def __init__(self, fail_after=None):
        self.sent = []
        self.fail_after = fail_after

    def call(self, cmd, query=(), **kw):
        if cmd == '/tool/sms/send':
            if self.fail_after is not None and len(self.sent) >= self.fail_after:
                raise ConnectionError('RouterOS закрыл соединение')
            self.sent.append((kw['phone-number'], kw['message']))
        return []

    def at(self, command, wait=False):
        return 'OK'

    def close(self):
        pass


def make_store(tmp):
    store = Store(Path(tmp) / 'archive.sqlite')
    store.initialize()
    return store


def add(store, n, sender='T2', text='Привет', concat=None, stamp='2026-09-23T10:00:00+03:00'):
    store.add_sms(f'h{n}', f'RAW{n}', {'sender': sender, 'text': text, 'timestamp': stamp, 'concat': concat, 'dcs': 8})


class Gsm7Test(unittest.TestCase):
    def test_translit_and_replacements(self):
        self.assertEqual(to_gsm7('Щука «ёж» — 5€ 🙂'), 'Shchuka "yozh" - 5€ ?')

    def test_short_message_is_one_sms(self):
        parts = forward_parts({'sender': 'T2', 'text': 'Баланс 400 руб.', 'timestamp': '2026-09-23T10:05:00+03:00'}, 3)
        self.assertEqual(parts, ['SMS ot T2 23.09 10:05:\nBalans 400 rub.'])

    def test_long_message_is_split_and_capped(self):
        text = 'Длинное сообщение. ' * 60
        parts = forward_parts({'sender': '900', 'text': text, 'timestamp': ''}, 3)
        self.assertEqual(len(parts), 3)
        self.assertTrue(parts[0].startswith('(1/3) '))
        self.assertTrue(parts[-1].endswith('...'))
        self.assertTrue(all(units(p) <= 160 for p in parts))

    def test_message_up_to_160_units_is_not_split(self):
        body = 'x' * (160 - len('SMS ot 1 :\n'))
        parts = forward_parts({'sender': '1', 'text': body, 'timestamp': ''}, 3)
        self.assertEqual(len(parts), 1)
        self.assertEqual(units(parts[0]), 160)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_concat_parts_are_one_unread_message_until_marked(self):
        add(self.store, 1, text='Часть 1 ', concat=[7, 2, 1])
        add(self.store, 2, text='часть 2', concat=[7, 2, 2])
        [m] = self.store.messages()
        self.assertEqual(m['text'], 'Часть 1 часть 2')
        self.assertEqual((m['parts'], m['complete'], m['read']), ('2/2', True, False))
        self.assertEqual(self.store.mark_read([max(m['ids'])]), 2)
        self.assertTrue(self.store.messages()[0]['read'])
        self.store.mark_read([m['id']], read=False)
        self.assertFalse(self.store.messages()[0]['read'])

    def test_missing_part_is_marked_incomplete(self):
        add(self.store, 1, text='A', concat=[9, 3, 1])
        [m] = self.store.messages()
        self.assertFalse(m['complete'])
        self.assertIn('[Ожидается часть 2]', m['text'])

    def test_existing_archive_is_migrated_as_read(self):
        path = Path(self.tmp.name) / 'old.sqlite'
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE sms (id INTEGER PRIMARY KEY, hash TEXT UNIQUE, raw TEXT NOT NULL, data TEXT NOT NULL, added REAL NOT NULL)')
            c.execute('INSERT INTO sms(hash,raw,data,added) VALUES(?,?,?,?)',
                      ('x', 'RAW', json.dumps({'sender': 'T2', 'text': 'old', 'timestamp': '', 'concat': None}), 1.0))
        c.close()
        old = Store(path)
        old.initialize()
        self.assertTrue(old.messages()[0]['read'])


class ForwardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)
        self.router = FakeRouter()
        self.gw = Gateway(self.store, lambda: self.router)

    def tearDown(self):
        self.tmp.cleanup()

    def enable(self, since, number='+79990000000'):
        self.store.save_forward_settings({'enabled': True, 'number': number, 'max_parts': 3, 'since': since})

    def test_forwards_new_messages_once(self):
        self.enable(time.time() - 60)
        add(self.store, 1, sender='+79161234567', text='Код 1234')
        self.gw.process_forwards(self.router)
        self.gw.process_forwards(self.router)
        self.assertEqual(self.router.sent, [('+79990000000', 'SMS ot +79161234567 23.09 10:00:\nKod 1234')])
        self.assertEqual(self.store.messages()[0]['forward']['status'], 'done')

    def test_backlog_before_enabling_is_not_forwarded(self):
        add(self.store, 1)
        self.enable(time.time() + 60)
        self.gw.process_forwards(self.router)
        self.assertEqual(self.router.sent, [])

    def test_messages_from_target_number_are_skipped(self):
        self.enable(time.time() - 60, number='+79990000000')
        add(self.store, 1, sender='89990000000')
        self.gw.process_forwards(self.router)
        self.assertEqual(self.router.sent, [])
        self.assertEqual(self.store.messages()[0]['forward']['status'], 'skipped')

    def test_send_error_is_recorded_and_not_retried(self):
        self.enable(time.time() - 60)
        add(self.store, 1, text='a')
        add(self.store, 2, text='b', stamp='2026-09-23T10:01:00+03:00')
        self.router.fail_after = 0
        self.gw.process_forwards(self.router)
        statuses = [m['forward'] and m['forward']['status'] for m in self.store.messages()]
        self.assertEqual(sorted(statuses, key=str), [None, 'error'])
        self.router.fail_after = None
        self.gw.process_forwards(self.router)
        self.assertEqual(len(self.router.sent), 1)

    def test_disabled_forwarding_sends_nothing(self):
        add(self.store, 1)
        self.gw.process_forwards(self.router)
        self.assertEqual(self.router.sent, [])


def local(y, m, d, hh=12):
    return time.mktime((y, m, d, hh, 0, 0, 0, 0, -1))


class LimitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)
        self.router = FakeRouter()
        self.gw = Gateway(self.store, lambda: self.router)

    def tearDown(self):
        self.tmp.cleanup()

    def test_period_bounds(self):
        self.assertEqual(period_bounds(5, local(2026, 9, 3)), (local(2026, 8, 5, 0), local(2026, 9, 5, 0)))
        self.assertEqual(period_bounds(5, local(2026, 9, 5)), (local(2026, 9, 5, 0), local(2026, 10, 5, 0)))
        self.assertEqual(period_bounds(10, local(2027, 1, 2)), (local(2026, 12, 10, 0), local(2027, 1, 10, 0)))
        self.assertEqual(period_bounds(1, local(2026, 12, 31)), (local(2026, 12, 1, 0), local(2027, 1, 1, 0)))

    def test_each_confirmed_sms_counts_and_quota_blocks(self):
        self.store.save_limit_settings({'monthly': 3, 'reset_day': 1})
        self.gw.send_parts(self.router, '+79990000000', ['a', 'b'])
        self.assertEqual(self.gw.limit_status()['used'], 2)
        with self.assertRaises(gateway.LimitReached):
            self.gw.send_parts(self.router, '+79990000000', ['c', 'd'])
        self.gw.send_sms(self.router, '+79990000000', 'e')
        with self.assertRaises(gateway.LimitReached):
            self.gw.send_sms(self.router, '+79990000000', 'f')
        self.assertEqual(len(self.router.sent), 3)
        self.assertEqual(self.gw.limit_status()['remaining'], 0)

    def test_zero_means_unlimited(self):
        for i in range(5):
            self.gw.send_sms(self.router, '+79990000000', str(i))
        self.assertIsNone(self.gw.limit_status()['remaining'])

    def test_forward_over_quota_is_marked_not_queued(self):
        self.store.save_limit_settings({'monthly': 1, 'reset_day': 1})
        self.store.log_sent(1)
        self.store.save_forward_settings({'enabled': True, 'number': '+79990000000', 'max_parts': 3, 'since': time.time() - 60})
        add(self.store, 1, sender='+79161234567')
        self.gw.process_forwards(self.router)
        self.assertEqual(self.router.sent, [])
        self.assertEqual(self.store.messages()[0]['forward']['status'], 'limit')
        self.assertIn('лимит', self.gw.state['forward_note'].lower())

    def test_existing_sends_are_counted_on_migration(self):
        path = Path(self.tmp.name) / 'old.sqlite'
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE operations (id TEXT PRIMARY KEY, kind TEXT, request TEXT, status TEXT, result TEXT, added REAL)')
            c.execute("INSERT INTO operations VALUES ('a','send','{}','done',NULL,?)", (time.time(),))
            c.execute("INSERT INTO operations VALUES ('b','forward','{\"parts\":[\"x\",\"y\"]}','done',NULL,?)", (time.time(),))
            c.execute("INSERT INTO operations VALUES ('c','send','{}','error',NULL,?)", (time.time(),))
        c.close()
        old = Store(path)
        old.initialize()
        self.assertEqual(old.sent_count(0), 3)


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)
        self.router = FakeRouter()
        self.gw = Gateway(self.store, lambda: self.router)
        cfg = {'api_tokens': [{'name': 'ci', 'sha256': server.token_hash('secret-token')}]}
        server.Handler.app = self.app = server.App(cfg, self.store, self.gw)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        add(self.store, 1, sender='+79161234567', text='Привет')

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read() or b'null')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'null')

    def auth(self, token='secret-token'):
        return {'Authorization': 'Bearer ' + token}

    def test_api_requires_valid_token(self):
        self.assertEqual(self.call('GET', '/api/v1/messages')[0], 401)
        self.assertEqual(self.call('GET', '/api/v1/messages', headers=self.auth('wrong'))[0], 401)
        code, body = self.call('GET', '/api/v1/messages?unread=1', headers=self.auth())
        self.assertEqual((code, body['total'], body['messages'][0]['text']), (200, 1, 'Привет'))

    def test_api_marks_read(self):
        mid = self.call('GET', '/api/v1/messages', headers=self.auth())[1]['messages'][0]['id']
        self.assertEqual(self.call('POST', '/api/v1/messages/read', {'ids': [mid]}, self.auth())[0], 200)
        self.assertTrue(self.call('GET', f'/api/v1/messages/{mid}', headers=self.auth())[1]['read'])

    def test_api_send_validates_and_is_idempotent(self):
        code, body = self.call('POST', '/api/v1/send', {'number': '123', 'text': 'Кириллица'}, self.auth())
        self.assertEqual(code, 400)
        payload = {'id': 'test-operation-0001', 'number': '+79990000000', 'text': 'Hello'}
        code, body = self.call('POST', '/api/v1/send', payload, self.auth())
        self.assertEqual(code, 202)
        for _ in range(50):
            op = self.call('GET', '/api/v1/operations/test-operation-0001', headers=self.auth())[1]
            if op['status'] != 'pending':
                break
            time.sleep(0.05)
        self.assertEqual(op['status'], 'done')
        self.assertEqual(op['request']['via'], 'api:ci')
        self.assertEqual(self.call('POST', '/api/v1/send', payload, self.auth())[0], 200)
        self.assertEqual(self.router.sent, [('+79990000000', 'Hello')])

    def test_api_send_over_limit_is_rejected(self):
        csrf = self.call('GET', '/api/state')[1]['csrf']
        self.assertEqual(self.call('POST', '/api/limits', {'monthly': 1, 'reset_day': 40}, {'X-CSRF-Token': csrf})[0], 400)
        code, limits = self.call('POST', '/api/limits', {'monthly': 1, 'reset_day': 5}, {'X-CSRF-Token': csrf})
        self.assertEqual((code, limits['monthly'], limits['used']), (200, 1, 0))
        self.store.log_sent(1)
        code, body = self.call('POST', '/api/v1/send', {'number': '+79990000000', 'text': 'Hi'}, self.auth())
        self.assertEqual(code, 429)
        self.assertIn('лимит', body['error'].lower())
        self.assertEqual(self.call('GET', '/api/v1/limits', headers=self.auth())[1]['remaining'], 0)

    def test_ui_post_requires_csrf(self):
        self.assertEqual(self.call('POST', '/api/read', {'ids': [1]})[0], 403)
        csrf = self.call('GET', '/api/state')[1]['csrf']
        self.assertEqual(self.call('POST', '/api/read', {'ids': [1]}, {'X-CSRF-Token': csrf})[0], 200)

    def test_forwarding_settings_start_from_now(self):
        csrf = self.call('GET', '/api/state')[1]['csrf']
        code, s = self.call('POST', '/api/forwarding', {'enabled': True, 'number': '+79990000000', 'max_parts': 2},
                            {'X-CSRF-Token': csrf})
        self.assertEqual((code, s['enabled']), (200, True))
        self.assertGreater(s['since'], time.time() - 5)
        self.assertEqual(self.call('POST', '/api/forwarding', {'enabled': True, 'number': 'abc'},
                                   {'X-CSRF-Token': csrf})[0], 400)

    def test_static_files_do_not_escape_directory(self):
        self.assertEqual(self.call('GET', '/static/..%2Fserver.py')[0], 404)
        self.assertEqual(self.call('GET', '/api/health'), (200, {'ok': True}))


if __name__ == '__main__':
    unittest.main()
