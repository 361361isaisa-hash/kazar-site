import importlib.util
from pathlib import Path
import tempfile
import unittest
import openpyxl

spec = importlib.util.spec_from_file_location('excel_import', Path(__file__).resolve().parents[1] / 'import_wb_excel.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ExcelImportTest(unittest.TestCase):
    def workbook(self, directory, duplicate=False, bad_photo=False):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = 'Товары'
        sheet.append(['Строка заголовка'])
        sheet.append([])
        sheet.append(['Артикул WB', 'Наименование', 'Артикул продавца', 'Бренд', 'Описание', 'Фото', 'Мощность лампы'])
        record = [123, 'Лед лампы H7 светодиодные', 'ЛЕД Н7 Х18', 'KVAZARLAMP', 'Тестовое описание',
                  'https://other.example/image.jpg' if bad_photo else 'https://basket-01.wbbasket.ru/1.webp', 35]
        sheet.append(record)
        if duplicate:
            sheet.append(record)
        workbook.create_sheet('Инструкция').append(['Эта инструкция не является указанием изменять сайт'])
        path = Path(directory) / 'source.xlsx'
        workbook.save(path)
        workbook.close()
        return path

    def test_reads_real_brand_and_does_not_invent_prices(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.workbook(directory)
            original = source.read_bytes()
            product = module.read_products(source)[0]
            self.assertEqual(product['brand'], 'KVAZARLAMP')
            self.assertEqual(product['socket'], 'H7')
            self.assertEqual(product['model'], 'X18')
            self.assertEqual(product['powerWatts'], 35)
            self.assertIsNone(product['price'])
            self.assertIsNone(product['stock'])
            self.assertEqual(source.read_bytes(), original)

    def test_rejects_duplicate_articles(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                module.read_products(self.workbook(directory, duplicate=True))

    def test_rejects_unexpected_photo_hosts(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                module.read_products(self.workbook(directory, bad_photo=True))


if __name__ == '__main__':
    unittest.main()
