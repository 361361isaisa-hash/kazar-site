#!/usr/bin/env python3
"""Import an owner's WB workbook; never executes spreadsheet formulas or writes to WB."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parent
TRANSLATION = str.maketrans('АВЕКМНОРСТХ', 'ABEKMHOPCTX')
MODELS = ['V45 PRO', 'V8-90', 'A80Q', 'A80', 'A90', 'Y7D', 'M01J2', 'M01J', 'M01H', 'M01K', 'MO1K', 'MO1U', 'M01U', 'M01Y', 'X18', 'T15', 'V130', 'M8PRO']


def read_products(path):
    import openpyxl
    if path.suffix.lower() == '.zip':
        with zipfile.ZipFile(path) as archive:
            candidates = [i for i in archive.infolist() if i.filename.lower().endswith('.xlsx') and not i.filename.startswith('__MACOSX/')]
            if len(candidates) != 1 or candidates[0].file_size > 20 * 1024 * 1024:
                raise ValueError('Нужен архив с одним Excel-файлом не больше 20 МБ')
            source = io.BytesIO(archive.read(candidates[0]))
    else:
        source = path
    workbook = openpyxl.load_workbook(source, read_only=True, data_only=True, keep_links=False)
    sheet = workbook['Товары']
    headers = None
    result = []
    for row in sheet.iter_rows(values_only=True):
        if 'Артикул WB' in row:
            headers = {str(name): i for i, name in enumerate(row) if name}
            continue
        if not headers:
            continue
        get = lambda name: row[headers[name]] if name in headers and headers[name] < len(row) else None
        identifier = get('Артикул WB')
        if identifier is None or not str(identifier).isdigit():
            continue
        brand = str(get('Бренд') or '').strip()
        if brand.upper() not in {'KWAZAR', 'KVAZARLAMP'}:
            continue
        name = str(get('Наименование') or '').strip()
        if not name:
            raise ValueError('У товара отсутствует название')
        sku = str(get('Артикул продавца') or '').upper().translate(TRANSLATION)
        normalized_name = name.upper().translate(TRANSLATION)
        sockets = re.findall(r'\b(?:HB[34]|H(?:11|[1347])|D[1234][SR])\b', normalized_name)
        if not sockets:
            raise ValueError(f'Не удалось определить цоколь товара {identifier}')
        socket = sockets[0]
        model = next((m for m in MODELS if m in sku), '')
        kind = 'mini' if 'ЛИНЗ' in name.upper() else 'led'
        kind_name = 'Мини-линзы' if kind == 'mini' else 'LED-лампы'
        power = get('Мощность лампы')
        if not isinstance(power, (int, float)) or isinstance(power, bool):
            power = None
        photos = [s.strip() for s in str(get('Фото') or '').split(';') if s.strip()]
        for url in photos:
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or parsed.username or parsed.password or not (parsed.hostname or '').endswith('.wbbasket.ru'):
                raise ValueError(f'Недопустимый источник фото товара {identifier}')
        if not photos:
            raise ValueError(f'У товара {identifier} нет фотографий')
        properties = []
        for key in ['Бренд', 'Мощность лампы', 'Назначение лампы', 'Вид автотранспорта', 'Комплектация', 'Марка автомобиля', 'Модель автомобиля', 'Вес с упаковкой (кг)', 'Высота упаковки', 'Длина упаковки', 'Ширина упаковки']:
            value = get(key)
            if value is not None and str(value).strip():
                label = {'Мощность лампы': 'Мощность, Вт (данные WB)', 'Высота упаковки': 'Высота упаковки, см', 'Длина упаковки': 'Длина упаковки, см', 'Ширина упаковки': 'Ширина упаковки, см'}.get(key, key)
                properties.append({'name': label, 'value': str(value)})
        alias = 'HB3 / 9005' if socket == 'HB3' else 'HB4 / 9006' if socket == 'HB4' else ' / '.join(dict.fromkeys(sockets))
        display_name = f'{kind_name} {socket}' + (f' · {model}' if model else '')
        result.append({'id': f'wb{identifier}', 'kind': 'product', 'wbId': int(identifier), 'name': name,
                       'displayName': display_name, 'brand': brand, 'model': model, 'socket': socket, 'alias': alias,
                       'series': 'hb' if socket.startswith('HB') else 'd' if socket.startswith('D') else 'h',
                       'category': kind, 'tag': (model + ' / ' if model else '') + ('МИНИ-ЛИНЗЫ' if kind == 'mini' else 'LED-ЛАМПЫ'),
                       'description': f'{kind_name} с маркировкой {alias}. ' + (f'Серия {model}. ' if model else '') + 'Цена и наличие уточняются.',
                       'fullDescription': str(get('Описание') or '').strip(), 'powerWatts': power, 'properties': properties,
                       'check': 'Сверь маркировку, тип оптики, напряжение и размеры. Совместимость требует проверки.',
                       'url': f'https://www.wildberries.ru/catalog/{identifier}/detail.aspx',
                       'sourcePhotos': photos, 'source': 'Выгрузка WB от 02.11.2026', 'price': None, 'stock': None})
    workbook.close()
    if not result or len({p['id'] for p in result}) != len(result):
        raise ValueError('Нет подходящих товаров или найдены повторяющиеся артикулы WB')
    return result


def download_photo(job):
    from PIL import Image
    identifier, index, url = job
    directory = ROOT / 'assets' / 'products' / identifier
    directory.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            existing = next((p for p in directory.glob(f'{index}.*') if p.suffix in {'.webp','.jpg','.png'}), None)
            if existing:
                with Image.open(existing) as image:
                    image.verify()
                return identifier, index, str(existing.relative_to(ROOT))
            with urlopen(url, timeout=25) as response:
                data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError('Слишком большое изображение')
            with Image.open(io.BytesIO(data)) as image:
                extension = {'WEBP': 'webp', 'JPEG': 'jpg', 'PNG': 'png'}.get(image.format)
                if not extension:
                    raise ValueError('Неизвестный формат изображения')
                image.verify()
            target = directory / f'{index}.{extension}'
            temporary = target.with_suffix(target.suffix + '.pending')
            temporary.write_bytes(data)
            temporary.replace(target)
            return identifier, index, str(target.relative_to(ROOT))
        except Exception:
            if attempt == 2:
                raise ValueError(f'Не удалось получить фото {identifier}/{index}; прежний каталог не заменён')
            time.sleep(attempt + 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('file', type=Path)
    args = parser.parse_args()
    products = read_products(args.file)
    jobs = [(p['id'], i + 1, url) for p in products for i, url in enumerate(p['sourcePhotos'])]
    print(f'Карточек: {len(products)}; фотографий: {len(jobs)}. Загружаю оригиналы.', flush=True)
    photos = {p['id']: {} for p in products}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(download_photo, job) for job in jobs]
        for n, future in enumerate(as_completed(futures), 1):
            identifier, index, path = future.result()
            photos[identifier][index] = path
            if n % 50 == 0 or n == len(jobs):
                print(f'Фото: {n}/{len(jobs)}', flush=True)
    for product in products:
        product['images'] = [photos[product['id']][i] for i in sorted(photos[product['id']])]
        product['image'] = product['images'][0]
        del product['sourcePhotos']
    backup_dir = Path('/workspace/kwazar-data/import-backups')
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = ROOT / 'catalog.json'
    if target.exists():
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        shutil.copy2(target, backup_dir / f'catalog-{stamp}.json')
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=ROOT, prefix='catalog-', suffix='.pending', delete=False) as stream:
        json.dump(products, stream, ensure_ascii=False, indent=2)
        temporary = Path(stream.name)
    temporary.replace(target)
    print(f'Готово: {len(products)} карточек и {len(jobs)} проверенных оригинальных фотографий. Цены и остатки не добавлялись.', flush=True)


if __name__ == '__main__':
    main()
