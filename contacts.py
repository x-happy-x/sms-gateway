"""Contact import: vCard (.vcf) and CSV exports from phones and Google Contacts."""
import csv
import io
import quopri
import re


def normalize_number(value):
    """E.164-like number, or None when the value is not a phone number."""
    raw = str(value or '').strip()
    digits = re.sub(r'\D', '', raw)
    if not 3 <= len(digits) <= 15:
        return None
    if raw.startswith('+'):
        return '+' + digits
    if len(digits) == 11 and digits[0] in '78':
        return '+7' + digits[1:]
    if len(digits) == 10 and digits[0] == '9':
        return '+7' + digits
    return digits


def number_key(number):
    return re.sub(r'\D', '', number or '')[-10:]


def _unfold(text):
    lines = []
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if line[:1] in (' ', '\t') and lines:
            lines[-1] += line[1:]
        elif lines and lines[-1].endswith('=') and 'QUOTED-PRINTABLE' in lines[-1].split(':', 1)[0].upper():
            # Quoted-printable soft line break.
            lines[-1] = lines[-1][:-1] + line
        else:
            lines.append(line)
    return lines


def _value(params, value):
    params = params.upper()
    if 'QUOTED-PRINTABLE' in params:
        charset = re.search(r'CHARSET=([\w-]+)', params)
        value = quopri.decodestring(value.encode('latin-1', 'replace')).decode(charset[1] if charset else 'utf-8', 'replace')
    return value.replace('\\,', ',').replace('\\;', ';').replace('\\n', ' ').replace('\\\\', '\\').strip()


def parse_vcard(text):
    contacts, card = [], None
    for line in _unfold(text):
        if ':' not in line:
            continue
        head, value = line.split(':', 1)
        name, _, params = head.partition(';')
        name = name.split('.')[-1].upper()  # item1.TEL -> TEL
        if name == 'BEGIN' and value.strip().upper() == 'VCARD':
            card = {'fn': '', 'n': '', 'org': '', 'tel': []}
        elif card is None:
            continue
        elif name == 'FN':
            card['fn'] = _value(params, value)
        elif name == 'N':
            parts = [p.strip() for p in _value(params, value).split(';')]
            card['n'] = ' '.join(p for p in (parts[1:2] + parts[2:3] + parts[:1]) if p)
        elif name == 'ORG':
            card['org'] = _value(params, value).replace(';', ' ').strip()
        elif name == 'TEL':
            card['tel'].append(_value(params, value))
        elif name == 'END' and value.strip().upper() == 'VCARD':
            label = card['fn'] or card['n'] or card['org']
            for tel in card['tel']:
                number = normalize_number(tel)
                if number:
                    contacts.append({'name': label or number, 'number': number})
            card = None
    return contacts


def parse_csv(text):
    text = text.lstrip('﻿')
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    name_cols = [i for i, h in enumerate(header) if h in ('name', 'имя', 'full name', 'display name', 'контакт', 'фио')]
    given = next((i for i, h in enumerate(header) if h in ('given name', 'first name', 'имя (личное)')), None)
    family = next((i for i, h in enumerate(header) if h in ('family name', 'last name', 'фамилия')), None)
    phone_cols = [i for i, h in enumerate(header)
                  if ('phone' in h or 'телефон' in h or h in ('номер', 'number', 'tel', 'mobile')) and 'type' not in h and 'label' not in h]
    if name_cols or phone_cols or given is not None:
        body = rows[1:]
    else:
        # No recognizable header: first column is the name, the second the number.
        body, name_cols, phone_cols = rows, [0], [1]
    contacts = []
    for row in body:
        cell = lambda i: row[i].strip() if i is not None and i < len(row) else ''
        name = next((cell(i) for i in name_cols if cell(i)), '') or ' '.join(x for x in (cell(given), cell(family)) if x)
        for i in phone_cols:
            # Google puts several numbers into one cell separated by " ::: ".
            for tel in re.split(r'\s*:::\s*|\s*[,;]\s*', cell(i)):
                number = normalize_number(tel)
                if number:
                    contacts.append({'name': name or number, 'number': number})
    return contacts


def parse_contacts(filename, text):
    if 'BEGIN:VCARD' in text[:2000].upper() or str(filename).lower().endswith('.vcf'):
        return parse_vcard(text)
    return parse_csv(text)
