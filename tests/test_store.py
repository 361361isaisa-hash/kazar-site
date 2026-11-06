"""Integration checks use temporary storage and a fake payment provider, never live money."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

spec = importlib.util.spec_from_file_location('kwazar', Path(__file__).resolve().parents[1] / 'server.py')
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)


class StoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='kwazar-tests-')
        store.DATA_DIR = Path(cls.temp.name)
        store.DB = store.DATA_DIR / 'orders.sqlite3'
        store.ADMIN_TOKEN = 'synthetic-test-owner-key-not-a-real-secret'
        store.initialize()
        store.Handler.log_message = lambda *args: None
        cls.server = store.ThreadingHTTPServer(('127.0.0.1', 0), store.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)
        cls.original_gateway = store.gateway

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    def setUp(self):
        store.RATE.clear()
        store.gateway = type(self).original_gateway
        store.SHOP_ID = store.PAYMENT_KEY = store.VAT_CODE = store.TAX_SYSTEM = ''
        store.PUBLIC_URL = ''
        os.environ.pop('KWAZAR_PAYMENTS_ENABLED', None)

    def req(self, path, payload=None, token=None, origin=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if origin:
            headers['Origin'] = origin
        req = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        try:
            with urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except HTTPError as r:
            if r.headers.get_content_type() == 'application/json':
                return r.code, json.load(r)
            return r.code, {}

    def create(self):
        payload = {'request_id': str(uuid.uuid4()), 'name': 'Тест', 'contact': 'test@example.invalid', 'city': 'Тест',
                   'comment': '', 'consent': True, 'vehicle': None, 'items': [{'id': 'h7', 'quantity': 2}]}
        code, order = self.req('/api/orders', payload)
        self.assertEqual(code, 201)
        return payload, order

    def test_owner_prices_persist_validate_and_clear(self):
        product = next(p for p in store.CATALOG.values() if p.get('kind') == 'product')
        payload = {'product_id': product['id'], 'price_kopeks': 123450}
        self.assertEqual(self.req('/api/admin/prices', payload)[0], 401)
        self.assertEqual(self.req('/api/admin/prices', payload, 'wrong-owner-token')[0], 401)
        for value in [0, -1, True, 1.5, '100', 1000000001]:
            self.assertEqual(self.req('/api/admin/prices', dict(payload, price_kopeks=value), store.ADMIN_TOKEN)[0], 400)
        self.assertEqual(self.req('/api/admin/prices', dict(payload, product_id='unknown'), store.ADMIN_TOKEN)[0], 400)
        self.assertEqual(self.req('/api/admin/prices', payload, store.ADMIN_TOKEN)[0], 200)
        store.initialize()
        products = self.req('/api/catalog')[1]
        self.assertEqual(next(p for p in products if p['id'] == product['id'])['price_kopeks'], 123450)
        self.assertEqual(self.req('/api/admin/prices', dict(payload, price_kopeks=None), store.ADMIN_TOKEN)[0], 200)
        self.assertIsNone(next(p for p in self.req('/api/catalog')[1] if p['id'] == product['id'])['price_kopeks'])

    def test_health_reports_storage_readiness_without_private_details(self):
        code, result = self.req('/healthz')
        self.assertEqual(code, 200)
        self.assertEqual(result, {'status': 'ok'})
        missing = store.DATA_DIR / 'missing.sqlite3'
        with patch.object(store, 'DB', missing):
            self.assertEqual(self.req('/healthz'), (503, {'status': 'unavailable'}))
        self.assertFalse(missing.exists())

    def test_forwarded_client_address_only_when_proxy_is_trusted(self):
        request = Request(self.base + '/api/orders', headers={'X-Forwarded-For': '203.0.113.42'})
        with patch.dict(os.environ, {'KWAZAR_TRUST_PROXY': '0'}):
            try:
                urlopen(request)
            except HTTPError:
                pass
            self.assertIn(('127.0.0.1', 'admin'), store.RATE)
            self.assertNotIn(('203.0.113.42', 'admin'), store.RATE)
        store.RATE.clear()
        with patch.dict(os.environ, {'KWAZAR_TRUST_PROXY': '1'}):
            try:
                urlopen(request)
            except HTTPError:
                pass
            self.assertIn(('203.0.113.42', 'admin'), store.RATE)

    def test_save_idempotency_and_access_control(self):
        payload, order = self.create()
        code, repeated = self.req('/api/orders', payload)
        self.assertEqual(code, 200)
        self.assertEqual(repeated, order)
        payload['name'] = 'Другая заявка'
        self.assertEqual(self.req('/api/orders', payload)[0], 409)
        path = '/api/orders/' + order['id']
        self.assertEqual(self.req(path)[0], 404)
        self.assertEqual(self.req('/api/orders')[0], 401)
        self.assertEqual(self.req(path, token=order['access_token'])[1]['details']['items'][0]['name'], 'Подбор ламп H7')
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM orders WHERE id=?', (order['id'],)).fetchone()[0], 1)
        for private in ['/server.py', '/README.md', '/../kwazar-data/admin-token', '/.git/config']:
            self.assertEqual(self.req(private)[0], 404)

    def test_real_product_is_snapshotted_from_server_catalog(self):
        product = next(p for p in store.CATALOG.values() if p.get('kind') == 'product')
        payload = {'request_id': str(uuid.uuid4()), 'name': 'Тест', 'contact': 'test@example.invalid', 'consent': True,
                   'items': [{'id': product['id'], 'quantity': 1, 'name': 'Поддельное название', 'price': 1}]}
        code, order = self.req('/api/orders', payload)
        self.assertEqual(code, 201)
        detail = self.req('/api/orders/' + order['id'], token=order['access_token'])[1]
        item = detail['details']['items'][0]
        self.assertEqual(item['name'], product['displayName'])
        self.assertEqual(item['wb_id'], product['wbId'])
        self.assertNotIn('price', item)

    def test_validation_and_cross_origin(self):
        payload, _ = self.create()
        payload['request_id'] = str(uuid.uuid4())
        payload['consent'] = False
        self.assertEqual(self.req('/api/orders', payload)[0], 400)
        payload['consent'] = True
        payload['items'][0]['quantity'] = -1
        self.assertEqual(self.req('/api/orders', payload)[0], 400)
        payload['items'][0] = {'id': 'fake', 'quantity': 1}
        self.assertEqual(self.req('/api/orders', payload)[0], 400)
        self.assertEqual(self.req('/api/orders', payload, origin='https://other.example')[0], 403)

    def test_quote_and_disabled_payments(self):
        _, order = self.create()
        path = '/api/orders/' + order['id']
        quote = {'amount_kopeks': 420050, 'description': 'Тестовый согласованный комплект'}
        self.assertEqual(self.req(path + '/quote', quote)[0], 401)
        self.assertEqual(self.req(path + '/quote', quote, store.ADMIN_TOKEN)[0], 200)
        detail = self.req(path, token=order['access_token'])[1]
        self.assertEqual(detail['quote_kopeks'], 420050)
        self.assertEqual(detail['status'], 'confirmed')
        self.assertFalse(detail['payment_available'])
        self.assertEqual(self.req(path + '/pay', {}, order['access_token'])[0], 503)
        self.assertEqual(self.req(path + '/quote', {'amount_kopeks': -1, 'description': 'x'}, store.ADMIN_TOKEN)[0], 400)

    def test_payment_uses_server_price_and_verifies_success(self):
        _, order = self.create()
        path = '/api/orders/' + order['id']
        self.req(path + '/quote', {'amount_kopeks': 590099, 'description': 'Тестовый комплект'}, store.ADMIN_TOKEN)
        os.environ['KWAZAR_PAYMENTS_ENABLED'] = '1'
        store.SHOP_ID, store.PAYMENT_KEY = 'synthetic-shop', 'synthetic-key'
        store.PUBLIC_URL, store.VAT_CODE, store.TAX_SYSTEM = 'https://shop.example', '1', '1'
        calls = []
        def fake_gateway(method, endpoint, payload=None, idempotency=None):
            calls.append((method, endpoint, payload, idempotency))
            if method == 'POST':
                return {'id': 'synthetic-payment-id', 'status': 'pending', 'confirmation': {'confirmation_url': 'https://yoomoney.ru/checkout/test'}}
            return {'id': 'synthetic-payment-id', 'status': 'succeeded', 'paid': True,
                    'amount': {'value': '5900.99', 'currency': 'RUB'}, 'metadata': {'order_id': order['id']}}
        store.gateway = fake_gateway
        code, reply = self.req(path + '/pay', {'amount_kopeks': 1}, order['access_token'])
        self.assertEqual(code, 200)
        self.assertTrue(reply['confirmation_url'].startswith('https://yoomoney.ru/'))
        self.assertEqual(calls[0][2]['amount']['value'], '5900.99')
        self.assertEqual(calls[0][2]['receipt']['customer']['email'], 'test@example.invalid')
        self.req(path + '/pay', {}, order['access_token'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.req(path + '/quote', {'amount_kopeks': 100, 'description': 'test'}, store.ADMIN_TOKEN)[0], 409)
        detail = self.req(path, token=order['access_token'])[1]
        self.assertEqual(detail['status'], 'paid')
        self.assertTrue(detail['paid_at'])

    def test_webhook_checks_provider_not_notification_status(self):
        _, order = self.create()
        with store.connection() as db:
            db.execute("UPDATE orders SET status='confirmed',quote_kopeks=250000,payment_id='test-webhook' WHERE id=?", (order['id'],))
        expected = {'id': 'test-webhook', 'status': 'pending', 'paid': False,
                    'amount': {'value': '2500.00', 'currency': 'RUB'}, 'metadata': {'order_id': order['id']}}
        store.gateway = lambda *args: expected
        notification = {'event': 'payment.succeeded', 'object': {'id': 'test-webhook', 'status': 'succeeded', 'paid': True}}
        self.assertEqual(self.req('/api/payments/webhook', notification)[0], 200)
        self.assertEqual(store.Handler.get_order(None, order['id'])['status'], 'confirmed')
        expected.update(status='succeeded', paid=True)
        self.assertEqual(self.req('/api/payments/webhook', notification)[0], 200)
        self.assertEqual(store.Handler.get_order(None, order['id'])['status'], 'paid')

    def test_mismatched_payment_is_not_marked_paid(self):
        _, order = self.create()
        with store.connection() as db:
            db.execute("UPDATE orders SET status='confirmed',quote_kopeks=10000,payment_id='test' WHERE id=?", (order['id'],))
        row = store.Handler.get_order(None, order['id'])
        with self.assertRaises(ValueError):
            store.verify_payment(row, {'id': 'test', 'status': 'succeeded', 'paid': True, 'metadata': {'order_id': order['id']},
                                       'amount': {'value': '1.00', 'currency': 'RUB'}})
        self.assertEqual(store.Handler.get_order(None, order['id'])['status'], 'confirmed')


if __name__ == '__main__':
    unittest.main()
