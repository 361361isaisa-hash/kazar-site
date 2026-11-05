#!/usr/bin/env python3
"""Read-only WB card export. Never prints tokens or writes to the WB account."""
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

API = 'https://content-api.wildberries.ru/content/v2/get/cards/list'
PAGE_SIZE = 100


def request_cards(token, cursor):
    payload = {'settings': {'sort': {'ascending': True}, 'cursor': cursor, 'filter': {'withPhoto': -1}}}
    request = Request(API, data=json.dumps(payload).encode(), method='POST',
                      headers={'Authorization': token, 'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=30) as response:
            data = json.load(response)
    except HTTPError as error:
        if error.code == 401:
            raise ValueError('WB: токен не принят. Проверь срок действия и подключение секрета')
        if error.code == 403:
            raise ValueError('WB: доступ запрещён. Проверь право чтения категории «Контент» и сетевой доступ')
        if error.code == 429:
            raise ValueError('WB: лимит запросов. Повтори синхронизацию позже; прежний экспорт сохранён')
        raise ValueError(f'WB: HTTP {error.code}; прежний экспорт сохранён')
    except (URLError, TimeoutError, OSError):
        raise ValueError('Не удалось подключиться к WB. Проверь сетевой доступ; прежний экспорт сохранён')
    if not isinstance(data, dict) or not isinstance(data.get('cards'), list) or not isinstance(data.get('cursor'), dict):
        raise ValueError('WB вернул неожиданный формат карточек; экспорт не изменён')
    return data


def photo_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    host = parsed.hostname or ''
    return parsed.scheme == 'https' and not parsed.username and not parsed.password and any(
        host == suffix or host.endswith('.' + suffix) for suffix in ('wbbasket.ru', 'wb.ru', 'wildberries.ru'))


def normalize(card):
    if not isinstance(card, dict) or re.sub(r'[^A-Z0-9]', '', str(card.get('brand', '')).upper()) not in {'KWAZAR', 'KVAZARLAMP'}:
        return None
    identifier = card.get('nmID')
    if type(identifier) is not int or identifier <= 0 or not isinstance(card.get('title'), str) or not card['title'].strip():
        raise ValueError('В карточке KWAZAR отсутствует артикул или название; экспорт не изменён')
    photos = []
    for photo in card.get('photos') or []:
        if isinstance(photo, dict):
            url = photo.get('big') or photo.get('c516x688')
            if photo_url(url) and url not in photos:
                photos.append(url)
    characteristics = []
    for entry in card.get('characteristics') or []:
        if isinstance(entry, dict) and isinstance(entry.get('name'), str):
            value = entry.get('value')
            if isinstance(value, (str, int, float, list)):
                characteristics.append({'name': entry['name'], 'value': value})
    return {'wb_id': identifier, 'name': card['title'].strip(), 'brand': card['brand'],
            'description': card.get('description') if isinstance(card.get('description'), str) else '',
            'characteristics': characteristics, 'photos': photos,
            'url': f'https://www.wildberries.ru/catalog/{identifier}/detail.aspx'}


def fetch_catalog(token):
    cursor, seen, products = {'limit': PAGE_SIZE}, set(), {}
    for _ in range(100):
        response = request_cards(token, cursor)
        for card in response['cards']:
            product = normalize(card)
            if product:
                products[product['wb_id']] = product
        next_cursor = response['cursor']
        total = next_cursor.get('total')
        if type(total) is not int or total < 0:
            raise ValueError('WB вернул некорректный курсор; экспорт не изменён')
        if total < PAGE_SIZE:
            break
        marker = (next_cursor.get('updatedAt'), next_cursor.get('nmID'))
        if not isinstance(marker[0], str) or type(marker[1]) is not int or marker in seen:
            raise ValueError('WB вернул повторный или неверный курсор; экспорт не изменён')
        seen.add(marker)
        cursor = {'limit': PAGE_SIZE, 'updatedAt': marker[0], 'nmID': marker[1]}
        time.sleep(0.7)
    else:
        raise ValueError('Достигнут предел страниц; неполный экспорт не сохранён')
    if not products:
        raise ValueError('В доступных карточках не найден бренд KWAZAR. Проверь магазин и доступ; экспорт не изменён')
    return list(products.values())


def save_export(products, directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / 'wb-catalog.json'
    temporary = directory / 'wb-catalog.pending.json'
    data = {'source': 'WB Content API', 'synced_at': datetime.now(timezone.utc).isoformat(), 'products': products}
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if target.exists():
            shutil.copy2(target, directory / 'wb-catalog.previous.json')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    token = os.environ.get('KWAZAR_WB_API_TOKEN', '')
    if not token:
        print('Подключение WB ожидает секрет KWAZAR_WB_API_TOKEN в защищённых настройках среды. Значение не отправлять в чат.', file=sys.stderr)
        return 2
    try:
        products = fetch_catalog(token)
        directory = Path(os.environ.get('KWAZAR_DATA_DIR', '/workspace/kwazar-data')).resolve()
        root = Path(__file__).resolve().parent
        if directory == root or root in directory.parents:
            raise ValueError('Экспорт кабинета должен находиться вне публичного checkout')
        save_export(products, directory)
        print(f'Получено карточек KWAZAR: {len(products)}. Экспорт сохранён вне публичного сайта. Перед публикацией нужно проверить данные и фотографии.')
        return 0
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
