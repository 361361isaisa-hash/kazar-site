import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import json

spec = importlib.util.spec_from_file_location('wb_sync', Path(__file__).resolve().parents[1] / 'wb_sync.py')
wb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wb)


class WBExportTest(unittest.TestCase):
    def card(self):
        return {'nmID': 123, 'brand': 'KWAZAR', 'title': 'Тестовая карточка', 'description': 'Тест',
                'photos': [{'big': 'https://basket-01.wbbasket.ru/example.jpg'}, {'big': 'https://other.example/photo.jpg'}],
                'characteristics': [{'name': 'Цоколь', 'value': ['H7']}]}

    def test_only_brand_and_trusted_photos(self):
        product = wb.normalize(self.card())
        self.assertEqual(len(product['photos']), 1)
        self.assertEqual(product['characteristics'][0]['value'], ['H7'])
        self.assertEqual(product['url'], 'https://www.wildberries.ru/catalog/123/detail.aspx')
        card = self.card(); card['brand'] = 'Другой бренд'
        self.assertIsNone(wb.normalize(card))
        self.assertFalse(wb.photo_url('https://wbbasket.ru.evil.example/image.jpg'))

    def test_complete_export_and_missing_brand(self):
        with patch.object(wb, 'request_cards', return_value={'cards': [self.card()], 'cursor': {'total': 1}}):
            products = wb.fetch_catalog('synthetic-test-token')
        with tempfile.TemporaryDirectory() as directory:
            wb.save_export(products, Path(directory))
            wb.save_export(products, Path(directory))
            data = json.loads((Path(directory) / 'wb-catalog.json').read_text())
            self.assertEqual(data['products'][0]['wb_id'], 123)
            self.assertTrue((Path(directory) / 'wb-catalog.previous.json').is_file())
        with patch.object(wb, 'request_cards', return_value={'cards': [], 'cursor': {'total': 0}}):
            with self.assertRaises(ValueError):
                wb.fetch_catalog('synthetic-test-token')

    def test_incomplete_pagination_rejected(self):
        page = {'cards': [self.card()], 'cursor': {'total': 100, 'updatedAt': 'test-date', 'nmID': 123}}
        with patch.object(wb, 'request_cards', return_value=page), patch.object(wb.time, 'sleep'):
            with self.assertRaises(ValueError):
                wb.fetch_catalog('synthetic-test-token')


if __name__ == '__main__':
    unittest.main()
