import hashlib
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import Gateway  # noqa: E402
from pdu import GSM, decode  # noqa: E402
from test_gateway import FakeRouter, add, make_store  # noqa: E402

MSK = timezone(timedelta(hours=3))


def bcd(v):
    return (v % 10) << 4 | v // 10


def address(number):
    digits = number.lstrip('+')
    padded = digits + ('F' if len(digits) % 2 else '')
    return bytes([len(digits), 0x91]) + bytes.fromhex(''.join(padded[i + 1] + padded[i] for i in range(0, len(padded), 2)))


def stamp(dt):
    return bytes(bcd(v) for v in (dt.year - 2000, dt.month, dt.day, dt.hour, dt.minute, dt.second)) + bytes([0x21])  # +03:00


def pack7(text):
    n = 0
    for i, ch in enumerate(text):
        n |= GSM.index(ch) << (7 * i)
    return n.to_bytes((7 * len(text) + 7) // 8, 'little')


def submit_pdu(number, text, mr=5):
    return ('00' + (bytes([0x11, mr]) + address(number) + bytes([0, 0, 0xA7, len(text)]) + pack7(text)).hex()).upper()


def report_pdu(number, sent_at, status, mr=5, delivered_after=20):
    scts = datetime.fromtimestamp(sent_at, MSK).replace(microsecond=0)
    body = bytes([0x06, mr]) + address(number) + stamp(scts) + stamp(scts + timedelta(seconds=delivered_after)) + bytes([status])
    return ('00' + body.hex()).upper()


class DecodeTest(unittest.TestCase):
    def test_submit_copy(self):
        d = decode(submit_pdu('+79990000000', 'Hello'))
        self.assertEqual((d['submit'], d['recipient'], d['text']), (True, '+79990000000', 'Hello'))

    def test_status_reports(self):
        ok = decode(report_pdu('+79990000000', time.time(), 0x00))
        self.assertEqual((ok['status_report'], ok['recipient'], ok['state'], ok['text']), (True, '+79990000000', 'delivered', 'доставлено'))
        self.assertEqual(decode(report_pdu('+79990000000', time.time(), 0x21))['state'], 'pending')
        self.assertEqual(decode(report_pdu('+79990000000', time.time(), 0x45))['state'], 'failed')


class RecordingRouter(FakeRouter):
    def __init__(self, sim=()):
        super().__init__()
        self.sim = dict(enumerate(sim, 1))
        self.kw = []
        self.deleted = []

    def call(self, cmd, query=(), **kw):
        self.kw.append(kw)
        return super().call(cmd, query, **kw)

    def at(self, command, wait=False):
        if command == 'AT+CMGL=4':
            return ''.join(f'+CMGL: {slot},1,,{len(raw) // 2}\r\n{raw}\r\n' for slot, raw in self.sim.items()) + 'OK'
        if command.startswith('AT+CMGR='):
            raw = self.sim.get(int(command.split('=')[1]))
            return f'+CMGR: 1,,0\r\n{raw}\r\nOK' if raw else 'OK'
        if command.startswith('AT+CMGD='):
            self.deleted.append(self.sim.pop(int(command.split('=')[1])))
            return 'OK'
        if command == 'AT+CPMS?':
            return f'+CPMS: "SM",{len(self.sim)},10,"SM",{len(self.sim)},10\r\nOK'
        return '+CMGF: 0\r\nOK'


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def add_raw(self, raw):
        self.store.add_sms(hashlib.sha256(raw.encode()).hexdigest(), raw, decode(raw))

    def test_send_requests_report_and_logs_number(self):
        router = RecordingRouter()
        Gateway(self.store, lambda: router).send_sms(router, '+79990000000', 'Hi', 'op-1')
        self.assertEqual(router.kw[-1]['status-report-request'], 'yes')
        self.assertEqual(self.store.deliveries()['op-1']['state'], 'pending')

    def test_reports_are_matched_by_number_and_time(self):
        now = time.time()
        self.store.log_sent(1, 'op-ok', '+79990000000', report=True)
        self.store.log_sent(1, 'op-multi', '+79161234567', report=True)
        self.store.log_sent(1, 'op-multi', '+79161234567', report=True)
        self.add_raw(report_pdu('+79990000000', now, 0x21, mr=1, delivered_after=5))
        self.add_raw(report_pdu('+79990000000', now, 0x00, mr=1, delivered_after=60))
        self.add_raw(report_pdu('+79161234567', now, 0x00, mr=2))
        self.add_raw(report_pdu('+79161234567', now + 1, 0x45, mr=3))
        d = self.store.deliveries()
        self.assertEqual((d['op-ok']['state'], d['op-ok']['delivered']), ('delivered', 1))
        self.assertEqual((d['op-multi']['state'], d['op-multi']['delivered'], d['op-multi']['failed']), ('failed', 1, 1))
        self.assertEqual(self.store.messages(), [])

    def test_report_for_another_number_or_time_does_not_match(self):
        self.store.log_sent(1, 'op', '+79990000000', report=True)
        self.add_raw(report_pdu('+79991112233', time.time(), 0x00))
        self.add_raw(report_pdu('+79990000000', time.time() - 7200, 0x00))
        self.assertEqual(self.store.deliveries()['op']['state'], 'pending')
        self.assertEqual(self.store.deliveries(now=time.time() + 4 * 86400)['op']['state'], 'unknown')

    def test_sim_copies_are_archived_hidden_and_released(self):
        sim = [submit_pdu('+79990000000', 'Copy'), report_pdu('+79990000000', time.time(), 0x00)]
        router = RecordingRouter(sim)
        add(self.store, 1, sender='+79161234567', text='Incoming')
        gw = Gateway(self.store, lambda: router)
        gw.archive(router)
        self.assertEqual(len(router.deleted), 2)
        self.assertEqual(gw.state['used'], 0)
        self.assertEqual([m['text'] for m in self.store.messages()], ['Incoming'])

    def test_old_decode_errors_are_redecoded(self):
        raw = submit_pdu('+79990000000', 'Old copy')
        self.store.add_sms(hashlib.sha256(raw.encode()).hexdigest(), raw,
                           {'sender': 'Неизвестный', 'text': 'Не удалось декодировать: Не SMS-DELIVER', 'timestamp': '',
                            'concat': None, 'decode_error': True})
        self.assertEqual(len(self.store.messages()), 1)
        Gateway(self.store, FakeRouter).redecode_failed()
        self.assertEqual(self.store.failed_rows(), [])
        self.assertEqual(self.store.messages(), [])


if __name__ == '__main__':
    unittest.main()
