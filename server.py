#!/usr/bin/env python3
"""KWAZAR development/storefront server. Standard library only; no card data stored."""
import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('KWAZAR_DATA_DIR', '/workspace/kwazar-data')).resolve()
DB = DATA_DIR / 'orders.sqlite3'
CATALOG = {p['id']: p for filename in ('selection.json', 'catalog.json') for p in json.loads((ROOT / filename).read_text())}
ADMIN_TOKEN = os.environ.get('KWAZAR_ADMIN_TOKEN', '')
PUBLIC_URL = os.environ.get('KWAZAR_PUBLIC_URL', '').rstrip('/')
SHOP_ID = os.environ.get('KWAZAR_YOOKASSA_SHOP_ID', '')
PAYMENT_KEY = os.environ.get('KWAZAR_YOOKASSA_SECRET_KEY', '')
VAT_CODE = os.environ.get('KWAZAR_RECEIPT_VAT_CODE', '')
TAX_SYSTEM = os.environ.get('KWAZAR_TAX_SYSTEM_CODE', '')
ORDER_MUTATION_LOCK = threading.RLock()
RATE_LOCK = threading.Lock()
RATE = {}
EMAIL = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
PURPOSES = {'Ближний свет', 'Дальний свет', 'Противотуманный свет', 'Не знаю — нужна помощь'}


def payment_ready():
    return bool(os.environ.get('KWAZAR_PAYMENTS_ENABLED') == '1' and SHOP_ID and PAYMENT_KEY
                and PUBLIC_URL.startswith('https://') and urlsplit(PUBLIC_URL).netloc
                and VAT_CODE.isdigit() and 1 <= int(VAT_CODE) <= 12
                and TAX_SYSTEM.isdigit() and 1 <= int(TAX_SYSTEM) <= 6)


def connection():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def initialize():
    global ADMIN_TOKEN
    os.umask(0o077)
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if DATA_DIR == ROOT or ROOT in DATA_DIR.parents:
        raise RuntimeError('KWAZAR_DATA_DIR must be outside the public checkout')
    if not ADMIN_TOKEN:
        key_file = DATA_DIR / 'admin-token'
        try:
            descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(secrets.token_urlsafe(36))
        except FileExistsError:
            pass
        ADMIN_TOKEN = key_file.read_text().strip()
    with connection() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('''CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
            access_token TEXT NOT NULL, reference TEXT NOT NULL, created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new', payload TEXT NOT NULL,
            quote_kopeks INTEGER, quote_description TEXT, quote_revision INTEGER NOT NULL DEFAULT 0,
            payment_id TEXT, payment_status TEXT, confirmation_url TEXT, paid_at TEXT)''')
    os.chmod(DB, 0o600)


def clean_text(value, maximum, required=False):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise ValueError('Некорректные или слишком длинные данные')
    value = value.strip()
    if required and not value:
        raise ValueError('Заполни обязательные поля')
    return value


def validate_order(data):
    if not isinstance(data, dict):
        raise ValueError('Некорректный формат заявки')
    try:
        request_id = str(uuid.UUID(str(data.get('request_id', ''))))
    except ValueError:
        raise ValueError('Некорректный идентификатор отправки')
    if data.get('consent') is not True:
        raise ValueError('Подтверди согласие на обработку заявки')
    contact = clean_text(data.get('contact'), 120, True)
    if not EMAIL.fullmatch(contact) and not re.fullmatch(r'\+?[\d\s()\-]{7,25}', contact):
        raise ValueError('Укажи корректный телефон или email')
    if not EMAIL.fullmatch(contact) and not 7 <= len(re.sub(r'\D', '', contact)) <= 15:
        raise ValueError('Проверь номер телефона')
    items = data.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= 12:
        raise ValueError('Добавь от 1 до 12 позиций')
    normalized, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Некорректная позиция')
        identifier, quantity = item.get('id'), item.get('quantity')
        if not isinstance(identifier, str) or identifier not in {*CATALOG, 'auto'} or identifier in seen:
            raise ValueError('Неизвестная или повторяющаяся позиция')
        if type(quantity) is not int or not 1 <= quantity <= 20:
            raise ValueError('Количество должно быть от 1 до 20')
        seen.add(identifier)
        normalized.append({'id': identifier, 'quantity': quantity,
                           'wb_id': CATALOG.get(identifier, {}).get('wbId'),
                           'name': (CATALOG[identifier].get('displayName') or f"Подбор ламп {CATALOG[identifier]['socket']}") if identifier in CATALOG else 'Подбор по автомобилю'})
    vehicle = data.get('vehicle')
    if vehicle is not None:
        if not isinstance(vehicle, dict) or type(vehicle.get('year')) is not int or not 1950 <= vehicle['year'] <= 2027:
            raise ValueError('Проверь год автомобиля')
        if vehicle.get('purpose') not in PURPOSES:
            raise ValueError('Укажи назначение света')
        vehicle = {'make': clean_text(vehicle.get('make'), 60, True), 'model': clean_text(vehicle.get('model'), 60, True),
                   'year': vehicle['year'], 'purpose': vehicle['purpose']}
    if 'auto' in seen and not vehicle:
        raise ValueError('Для подбора по автомобилю укажи его данные')
    payload = {'name': clean_text(data.get('name'), 80, True), 'contact': contact,
               'city': clean_text(data.get('city', ''), 100), 'comment': clean_text(data.get('comment', ''), 1000),
               'consent': True, 'consent_version': '2026-11-02', 'items': normalized, 'vehicle': vehicle}
    return request_id, payload


def gateway(method, path, payload=None, idempotency=None):
    if not payment_ready():
        raise ValueError('Онлайн-оплата ещё не подключена. Напиши на 361isa@mail.ru')
    headers = {'Authorization': 'Basic ' + base64.b64encode(f'{SHOP_ID}:{PAYMENT_KEY}'.encode()).decode(),
               'Content-Type': 'application/json'}
    if idempotency:
        headers['Idempotence-Key'] = idempotency
    request = Request('https://api.yookassa.ru/v3/' + path, data=json.dumps(payload).encode() if payload is not None else None,
                      headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            result = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        raise ValueError('Платёжный сервис сейчас недоступен. Попробуй позже; заказ не отмечен оплаченным')
    return result


def verify_payment(order, payment):
    expected = f"{order['quote_kopeks'] // 100}.{order['quote_kopeks'] % 100:02d}"
    if (payment.get('id') != order['payment_id'] or payment.get('metadata', {}).get('order_id') != order['id']
            or payment.get('amount', {}).get('value') != expected or payment.get('amount', {}).get('currency') != 'RUB'):
        raise ValueError('Не удалось проверить параметры платежа')
    if payment.get('status') == 'succeeded' and payment.get('paid') is True:
        with connection() as db:
            db.execute("UPDATE orders SET status='paid', payment_status='succeeded', paid_at=COALESCE(paid_at, ?) WHERE id=?",
                       (datetime.now(timezone.utc).isoformat(), order['id']))
    elif payment.get('status') == 'canceled':
        with connection() as db:
            db.execute("UPDATE orders SET payment_status='canceled' WHERE id=?", (order['id'],))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
        super().end_headers()

    def respond(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def is_admin(self):
        token = self.headers.get('Authorization', '')
        return len(ADMIN_TOKEN) >= 24 and hmac.compare_digest(token, 'Bearer ' + ADMIN_TOKEN)

    def authorized(self, order):
        return order and (self.is_admin() or hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + order['access_token']))

    def limited(self, action, limit=20):
        address = self.client_address[0]
        # Enable only behind a trusted proxy with no public access to app:8000.
        if os.environ.get('KWAZAR_TRUST_PROXY') == '1':
            forwarded = self.headers.get('X-Forwarded-For', '').split(',')[-1].strip()
            try:
                address = str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        key = (address, action)
        now = time.monotonic()
        with RATE_LOCK:
            if len(RATE) > 5000:
                for old in list(RATE):
                    if not RATE[old] or RATE[old][-1] < now - 60:
                        del RATE[old]
            history = [x for x in RATE.get(key, []) if x > now - 60]
            if len(history) >= limit:
                self.respond(429, {'error': 'Слишком много запросов. Подожди минуту'})
                return True
            RATE[key] = history + [now]
        return False

    def safe_static(self):
        path = unquote(urlsplit(self.path).path)
        allowed = {'/', '/index.html', '/styles.css', '/app.js', '/account.js', '/catalog.json', '/order.html', '/admin.html'}
        if path not in allowed:
            target = (ROOT / path.lstrip('/')).resolve()
            asset_root = (ROOT / 'assets').resolve()
            if asset_root not in target.parents or target.suffix.lower() not in {'.svg', '.jpg', '.jpeg', '.png', '.webp', '.ttf', '.woff2', '.txt'}:
                self.send_error(404)
                return False
        return True

    def do_HEAD(self):
        if self.safe_static():
            super().do_HEAD()

    def get_order(self, identifier):
        with connection() as db:
            return db.execute('SELECT * FROM orders WHERE id=?', (identifier,)).fetchone()

    def serialize(self, order, admin=False):
        payload = json.loads(order['payload'])
        out = {'id': order['id'], 'reference': order['reference'], 'created_at': order['created_at'], 'status': order['status'],
               'details': payload, 'quote_kopeks': order['quote_kopeks'], 'quote_description': order['quote_description'],
               'payment_status': order['payment_status'], 'paid_at': order['paid_at'], 'payment_available': payment_ready()}
        if admin:
            out['access_token'] = order['access_token']
        return out

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/healthz':
            try:
                with sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True, timeout=3) as db:
                    db.execute('SELECT 1 FROM orders LIMIT 1').fetchall()
                if not CATALOG or not (ROOT / 'index.html').is_file():
                    raise ValueError('Not ready')
                return self.respond(200, {'status': 'ok'})
            except (sqlite3.Error, OSError, ValueError):
                return self.respond(503, {'status': 'unavailable'})
        if path == '/api/orders':
            if self.limited('admin', 60):
                return
            if not self.is_admin():
                return self.respond(401, {'error': 'Нужен ключ владельца; доступ закрыт'})
            with connection() as db:
                rows = db.execute('SELECT * FROM orders ORDER BY created_at DESC LIMIT 200').fetchall()
            return self.respond(200, {'orders': [self.serialize(row, True) for row in rows]})
        match = re.fullmatch(r'/api/orders/([a-f0-9-]{36})', path)
        if match:
            if self.limited('read-order', 90):
                return
            order = self.get_order(match[1])
            if not self.authorized(order):
                return self.respond(404, {'error': 'Заявка не найдена или личная ссылка неверна'})
            payment_warning = None
            if order['payment_id'] and order['status'] != 'paid' and payment_ready():
                try:
                    verify_payment(order, gateway('GET', 'payments/' + order['payment_id']))
                    order = self.get_order(order['id'])
                except ValueError as error:
                    payment_warning = str(error)
            out = self.serialize(order)
            if payment_warning:
                out['payment_warning'] = payment_warning
            return self.respond(200, out)
        if path.startswith('/api/'):
            return self.respond(404, {'error': 'Не найдено'})
        if self.safe_static():
            super().do_GET()

    def do_POST(self):
        if self.limited('write', 20):
            return
        origin = self.headers.get('Origin')
        host = self.headers.get('Host', '')
        public_origin = urlsplit(PUBLIC_URL)
        if origin and (urlsplit(origin).scheme, urlsplit(origin).netloc) not in {('http', host), ('https', host), (public_origin.scheme, public_origin.netloc)}:
            return self.respond(403, {'error': 'Недопустимый источник запроса'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                return self.respond(413, {'error': 'Превышен размер запроса'})
            if self.headers.get_content_type() != 'application/json':
                return self.respond(415, {'error': 'Нужен формат JSON'})
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('Некорректный формат')
            path = urlsplit(self.path).path
            if path == '/api/payments/webhook':
                return self.payment_webhook(data)
            if path == '/api/orders':
                return self.create_order(data)
            match = re.fullmatch(r'/api/orders/([a-f0-9-]{36})/(quote|pay|cancel)', path)
            if not match:
                return self.respond(404, {'error': 'Не найдено'})
            with ORDER_MUTATION_LOCK:
                order = self.get_order(match[1])
                if match[2] in {'quote', 'cancel'}:
                    if not self.is_admin():
                        return self.respond(401, {'error': 'Нужен ключ владельца'})
                    if not order:
                        return self.respond(404, {'error': 'Заявка не найдена'})
                    return self.quote_order(order, data, match[2])
                if not self.authorized(order):
                    return self.respond(404, {'error': 'Заявка не найдена'})
                return self.create_payment(order)
        except (ValueError, TypeError, KeyError, UnicodeDecodeError) as error:
            return self.respond(400, {'error': str(error) if isinstance(error, ValueError) else 'Проверь поля заявки'})
        except sqlite3.Error:
            return self.respond(503, {'error': 'Не удалось сохранить данные. Попробуй позже'})

    def payment_webhook(self, data):
        # Notifications trigger an authenticated server-to-server status lookup.
        # Their event name, amount and claimed status are never trusted.
        identifier = data.get('object', {}).get('id') if isinstance(data.get('object'), dict) else None
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-zA-Z0-9-]{1,100}', identifier):
            raise ValueError('Invalid notification')
        with connection() as db:
            order = db.execute('SELECT * FROM orders WHERE payment_id=?', (identifier,)).fetchone()
        if not order or order['status'] == 'paid':
            return self.respond(200, {'ok': True})
        try:
            verify_payment(order, gateway('GET', 'payments/' + identifier))
        except ValueError:
            return self.respond(503, {'error': 'Payment verification unavailable'})
        return self.respond(200, {'ok': True})

    def create_order(self, data):
        request_id, payload = validate_order(data)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        identifier, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
        reference = 'KW-' + identifier[:8].upper()
        with connection() as db:
            # A single write transaction protects idempotent concurrent retries.
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM orders WHERE request_id=?', (request_id,)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    return self.respond(409, {'error': 'Эта отправка уже использована для другой заявки'})
                return self.respond(200, {'id': previous['id'], 'reference': previous['reference'], 'access_token': previous['access_token']})
            db.execute('INSERT INTO orders(id, request_id, fingerprint, access_token, reference, created_at, payload) VALUES (?,?,?,?,?,?,?)',
                       (identifier, request_id, fingerprint, token, reference, datetime.now(timezone.utc).isoformat(), json.dumps(payload, ensure_ascii=False)))
        return self.respond(201, {'id': identifier, 'reference': reference, 'access_token': token})

    def quote_order(self, order, data, action):
        with connection() as db:
            db.execute('BEGIN IMMEDIATE')
            fresh = db.execute('SELECT * FROM orders WHERE id=?', (order['id'],)).fetchone()
            if fresh['status'] == 'paid' or fresh['payment_id']:
                return self.respond(409, {'error': 'Есть платёж: изменения заблокированы. Проверь его в ЮKassa'})
            if action == 'cancel':
                db.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order['id'],))
            else:
                amount = data.get('amount_kopeks')
                if type(amount) is not int or not 100 <= amount <= 100000000:
                    raise ValueError('Стоимость должна быть от 1 до 1 000 000 рублей')
                description = clean_text(data.get('description'), 128, True)
                db.execute("UPDATE orders SET status='confirmed', quote_kopeks=?, quote_description=?, quote_revision=quote_revision+1 WHERE id=?",
                           (amount, description, order['id']))
        return self.respond(200, {'ok': True})

    def create_payment(self, order):
        if not payment_ready():
            return self.respond(503, {'error': 'Оплата ещё не подключена. Напиши на 361isa@mail.ru'})
        if order['status'] != 'confirmed' or not order['quote_kopeks']:
            return self.respond(409, {'error': 'Сначала нужно подтвердить товар и стоимость'})
        if order['payment_id']:
            return self.respond(200, {'confirmation_url': order['confirmation_url']})
        details = json.loads(order['payload'])
        contact = details['contact']
        if EMAIL.fullmatch(contact):
            customer = {'email': contact}
        else:
            digits = re.sub(r'\D', '', contact)
            if len(digits) == 11 and digits.startswith('8'):
                digits = '7' + digits[1:]
            if not 10 <= len(digits) <= 15:
                raise ValueError('Для чека нужен корректный телефон или email. Свяжись с менеджером')
            customer = {'phone': '+' + digits}
        amount = {'value': f"{order['quote_kopeks'] // 100}.{order['quote_kopeks'] % 100:02d}", 'currency': 'RUB'}
        payload = {'amount': amount, 'capture': True, 'description': f"KWAZAR {order['reference']}",
                   'confirmation': {'type': 'redirect', 'return_url': PUBLIC_URL + '/order.html#id=' + order['id']},
                   'metadata': {'order_id': order['id']},
                   'receipt': {'customer': customer, 'tax_system_code': int(TAX_SYSTEM), 'items': [
                       {'description': order['quote_description'], 'quantity': '1.00', 'amount': amount,
                        'vat_code': int(VAT_CODE), 'payment_mode': 'full_payment', 'payment_subject': 'commodity'}]}}
        payment = gateway('POST', 'payments', payload, str(uuid.uuid5(uuid.NAMESPACE_URL, f"kwazar:{order['id']}:{order['quote_revision']}")))
        url = payment.get('confirmation', {}).get('confirmation_url', '')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not any(parsed.hostname == host or (parsed.hostname or '').endswith('.' + host) for host in ('yookassa.ru', 'yoomoney.ru')):
            raise ValueError('Платёжный сервис не вернул безопасную ссылку оплаты')
        if not re.fullmatch(r'[a-zA-Z0-9-]{1,100}', str(payment.get('id', ''))):
            raise ValueError('Не удалось проверить идентификатор платежа')
        with connection() as db:
            db.execute('UPDATE orders SET payment_id=?, payment_status=?, confirmation_url=? WHERE id=?',
                       (payment['id'], payment.get('status'), url, order['id']))
        return self.respond(200, {'confirmation_url': url})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--bind', default='0.0.0.0')
    args = parser.parse_args()
    initialize()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f'KWAZAR listening on port {server.server_port}; persistent order storage initialized.', flush=True)
    server.serve_forever()
