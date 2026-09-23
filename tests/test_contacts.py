import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contacts import normalize_number, parse_contacts  # noqa: E402
from gateway import Gateway  # noqa: E402
from store import Store  # noqa: E402
from test_gateway import FakeRouter, HttpBase, add, make_store  # noqa: E402

ANDROID_VCF = (
    'BEGIN:VCARD\r\nVERSION:2.1\r\n'
    'N;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=D0=98=D0=B2=D0=B0=D0=BD=D0=BE=D0=B2;=D0=98=D0=B2=D0=B0=D0=BD;;;\r\n'
    'FN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=D0=98=D0=B2=D0=B0=D0=BD =D0=98=D0=B2=\r\n'
    '=D0=B0=D0=BD=D0=BE=D0=B2\r\n'
    'TEL;CELL:8 (916) 123-45-67\r\nTEL;WORK:+7 495 000-00-00\r\nEND:VCARD\r\n'
    'BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Mama\r\nitem1.TEL;type=CELL:9031112233\r\nEND:VCARD\r\n'
    'BEGIN:VCARD\r\nVERSION:3.0\r\nFN:No phone\r\nEMAIL:a@b.c\r\nEND:VCARD\r\n'
)

GOOGLE_CSV = ('Name,Given Name,Family Name,Phone 1 - Type,Phone 1 - Value\n'
              'Пётр,Пётр,,Mobile,+7 916 000-11-22 ::: 8 903 000 33 44\n'
              ',Anna,Smith,Mobile,+44 20 7946 0958\n')


class ParseTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_number('8 (916) 123-45-67'), '+79161234567')
        self.assertEqual(normalize_number('9161234567'), '+79161234567')
        self.assertEqual(normalize_number('+44 20 7946 0958'), '+442079460958')
        self.assertEqual(normalize_number('900'), '900')
        self.assertIsNone(normalize_number('ab'))

    def test_android_vcard(self):
        self.assertEqual(parse_contacts('contacts.vcf', ANDROID_VCF), [
            {'name': 'Иван Иванов', 'number': '+79161234567'},
            {'name': 'Иван Иванов', 'number': '+74950000000'},
            {'name': 'Mama', 'number': '+79031112233'},
        ])

    def test_google_csv(self):
        self.assertEqual(parse_contacts('google.csv', GOOGLE_CSV), [
            {'name': 'Пётр', 'number': '+79160001122'},
            {'name': 'Пётр', 'number': '+79030003344'},
            {'name': 'Anna Smith', 'number': '+442079460958'},
        ])

    def test_plain_csv_without_header(self):
        self.assertEqual(parse_contacts('list.csv', 'Мама;89031112233\nРабота;+74950000000\n'),
                         [{'name': 'Мама', 'number': '+79031112233'}, {'name': 'Работа', 'number': '+74950000000'}])


class StoreContactsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_number_is_rejected(self):
        cid = self.store.save_contact(None, 'Мама', '+79031112233', '', '9031112233')
        with self.assertRaisesRegex(ValueError, 'Мама'):
            self.store.save_contact(None, 'Другая', '89031112233', '', '9031112233')
        self.store.save_contact(cid, 'Мама Л.', '+79031112233', 'дача', '9031112233')
        self.assertEqual(self.store.contacts()[0]['name'], 'Мама Л.')

    def test_import_keeps_names_unless_overwrite(self):
        self.store.save_contact(None, 'Мама', '+79031112233', '', '9031112233')
        items = [('Mother', '+79031112233', '9031112233'), ('New', '+79160000000', '9160000000')]
        self.assertEqual(self.store.import_contacts(items), {'added': 1, 'updated': 0, 'skipped': 1})
        self.assertEqual(self.store.import_contacts(items, overwrite=True), {'added': 0, 'updated': 1, 'skipped': 1})
        self.assertEqual(sorted(c['name'] for c in self.store.contacts()), ['Mother', 'New'])

    def test_forward_uses_contact_name(self):
        self.store.save_contact(None, 'Мама', '+79031112233', '', '9031112233')
        add(self.store, 1, sender='+79031112233', text='Позвони')
        request = Gateway(self.store, FakeRouter).forward_request(self.store.messages()[0], {'number': '+79990000000', 'max_parts': 3})
        self.assertTrue(request['parts'][0].startswith('SMS ot Mama (+79031112233)'))


class ContactsHttpTest(HttpBase):
    def test_import_edit_delete_and_api_name(self):
        csrf = {'X-CSRF-Token': self.call('GET', '/api/state')[1]['csrf']}
        code, body = self.call('POST', '/api/contacts/import', {'filename': 'c.vcf', 'content': ANDROID_VCF}, csrf)
        self.assertEqual((code, body['added'], body['found']), (200, 3, 3))
        self.assertEqual(self.call('POST', '/api/contacts/import', {'filename': 'x.csv', 'content': 'nothing here'}, csrf)[0], 400)
        code, contact = self.call('POST', '/api/contacts/save', {'name': 'Друг', 'number': '89161234567'}, csrf)
        self.assertEqual(code, 400)  # already belongs to Иван Иванов
        ivan = next(c for c in self.store.contacts() if c['number'] == '+79161234567')
        code, contact = self.call('POST', '/api/contacts/save', {'id': ivan['id'], 'name': 'Друг', 'number': '89161234567', 'note': 'работа'}, csrf)
        self.assertEqual((code, contact['number']), (200, '+79161234567'))
        msg = self.call('GET', '/api/v1/messages', headers=self.auth())[1]['messages'][0]
        self.assertEqual(msg['sender_name'], 'Друг')
        self.assertEqual(len(self.call('GET', '/api/v1/contacts', headers=self.auth())[1]['contacts']), 3)
        self.assertEqual(self.call('POST', '/api/contacts/delete', {'ids': [ivan['id']]}, csrf)[1], {'deleted': 1})
        self.assertIsNone(self.call('GET', '/api/v1/messages', headers=self.auth())[1]['messages'][0]['sender_name'])


if __name__ == '__main__':
    unittest.main()
