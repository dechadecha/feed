# -*- coding: utf-8 -*-
"""Собирает YML-фид для Яндекс Директа.

YML (а не YRL) — потому что только в нём есть <oldprice>: Директ рисует по нему
шильдик со скидкой и показывает обе цены. В YRL старой цены нет вообще.

Устройство: адаптер источника превращает данные в нормализованные словари
(проект + лоты), сборщик XML работает только с ними и про источник не знает.

Источники (FEED_SOURCE в feed.conf):
  api  — открытый JSON API сайта-источника (по умолчанию)
  yrl  — готовый YRL-фид застройщика (выгрузка для классифайдов):
         конвертируем в YML, добавляя категории и custom_label
  csv  — таблица от клиента (CSV/выгрузка из Excel или Google Sheets):
         колонки ищем по названиям — обязательные должны быть,
         необязательные подтягиваем, если найдены

Конфиг feed.conf лежит рядом со скриптом (создаёт setup.sh), переменные
окружения его перекрывают:

  FEED_SOURCE        api | yrl                       (по умолчанию api)
  FEED_API           адрес API                        (для api)
  FEED_SITE          адрес сайта
  FEED_PROJECT       слаг проекта в API               (для api)
  FEED_YRL_URL       адрес готового YRL-фида          (для yrl)
  FEED_CSV_PATH      путь к CSV-файлу на сервере       (для csv)
  FEED_CSV_URL       или адрес CSV (напр. Google Sheets, опубликованный как csv)
  FEED_URL_TEMPLATE  шаблон ссылки на лот, {id} подставится — если в csv
                     нет колонки со ссылкой
  FEED_ADDRESS       адрес ЖК                          (для csv/yrl, опционально)
  FEED_NAME          название ЖК              (для yrl/csv; api берёт из API)
  FEED_COMPANY       компания в шапке фида; пусто — имя проекта
  FEED_CATALOG_URL   страница каталога                (для yrl; api строит сам)
  FEED_CATALOG_ROOMS_URL  шаблон страницы группы, {rooms} подставится
  FEED_PLAN_RECIPE   шаблон обработки планировки, {url} подставится;
                     пусто — рецепт imgproxy по умолчанию (см. plan_recipe)
  FEED_PHOTO_RECIPE  то же для фото ЖК
  FEED_BASE_URL      публичный адрес папки с фидом; пусто — картинки
                     не зеркалим, отдаём ссылки обработчика напрямую

Запускается по таймеру, пишет XML атомарно (tmp + rename), чтобы Директ
никогда не забрал наполовину записанный файл.

    python3 build_feed.py /var/www/html/feeds/feed.xml
"""
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr


def _load_conf():
    conf = {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'feed.conf')
    if os.path.exists(path):
        for ln in open(path, encoding='utf-8'):
            ln = ln.strip()
            if ln and not ln.startswith('#') and '=' in ln:
                k, v = ln.split('=', 1)
                conf[k.strip()] = v.strip()
    conf.update({k: v for k, v in os.environ.items() if k.startswith('FEED_')})
    return conf


_C = _load_conf()
SOURCE = _C.get('FEED_SOURCE', 'api').strip() or 'api'
API = _C.get('FEED_API', '').rstrip('/')
SITE = _C.get('FEED_SITE', '').rstrip('/')
PROJECT = _C.get('FEED_PROJECT', '')
YRL_URL = _C.get('FEED_YRL_URL', '')
COMPANY = _C.get('FEED_COMPANY', '')   # имя компании в шапке фида; пусто — имя проекта

if SOURCE == 'api' and not (API and SITE and PROJECT):
    raise SystemExit('Источник api: нужны FEED_API, FEED_SITE, FEED_PROJECT.\n'
                     'Запустите setup.sh — он спросит значения и сохранит feed.conf.')
if SOURCE == 'yrl' and not YRL_URL:
    raise SystemExit('Источник yrl: нужен FEED_YRL_URL — адрес готового YRL-фида.')
if SOURCE == 'csv' and not (_C.get('FEED_CSV_PATH') or _C.get('FEED_CSV_URL')):
    raise SystemExit('Источник csv: нужен FEED_CSV_PATH (файл) или FEED_CSV_URL.')
if SOURCE == 'csv' and not _C.get('FEED_NAME'):
    raise SystemExit('Источник csv: нужен FEED_NAME — название ЖК для шапки фида.')
if SOURCE not in ('api', 'yrl', 'csv'):
    raise SystemExit(f'Неизвестный FEED_SOURCE={SOURCE!r}, допустимо: api, yrl, csv')

OUT = sys.argv[1] if len(sys.argv) > 1 else 'feed.xml'
MSK = timezone(timedelta(hours=3))
REQUEST_PAUSE = 0.5  # секунд между запросами к источнику

BASE_URL = _C.get('FEED_BASE_URL', '').strip().rstrip('/')
IMG_DIR = os.path.join(os.path.dirname(os.path.abspath(OUT)), 'img')

USED = set()      # имена файлов, задействованные в этом прогоне
SKIPPED = []      # картинки, которые не удалось ни скачать, ни найти локально


def fetch(url, timeout=90):
    req = urllib.request.Request(url, headers={'User-Agent': 'feed-builder/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get(url):
    req = urllib.request.Request(url, headers={'Accept': 'application/json',
                                               'User-Agent': 'feed-builder/1.0'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode('utf-8'))


# ─── Рецепты картинок ────────────────────────────────────────────────────────
# Директ обрезает картинку под формат блока и не даёт этим управлять из фида,
# поэтому вокруг планировки нужны поля — иначе кроп режет сам план.
#
# Планировки почти все вертикальные (медиана ш/в ~0.68), и квадратное окно им
# не подходит: вписанный в квадрат вертикальный план занимает по ширине ~38%
# канвы и выглядит мелким. Поэтому окно прямоугольное, а до квадрата канва
# добирается асимметричными полями.
#
# Границы окна: ширина 56% канвы — столько оставляет центральный кроп 9:16,
# высота 86% — вертикальные и квадратные блоки её не трогают.
CANVAS = 1166           # сторона готовой картинки
PLAN_W = 650            # 56% — предел для вертикального кропа
PLAN_H = 1000           # 86% — предел для квадратного блока
PAD_V = (CANVAS - PLAN_H) // 2
PAD_H = (CANVAS - PLAN_W) // 2


def plan_recipe(url):
    """Планировка: вписать в окно с белыми полями, убрать прозрачность (@jpg).
    Шаблон берётся из FEED_PLAN_RECIPE ({url} подставится). Если он не задан —
    imgproxy-рецепт применяем только к api-источнику (там imgproxy заведомо
    есть); для yrl/csv картинку отдаём как есть — у них готовые ссылки."""
    tpl = _C.get('FEED_PLAN_RECIPE', '').strip()
    if tpl:
        return tpl.format(url=url)
    if SOURCE != 'api':
        return url
    return (f'{SITE}/proxy/insecure/w:{PLAN_W}/h:{PLAN_H}/rt:fit/ex:1'
            f'/pd:{PAD_V}:{PAD_H}/q:85/plain/{url}@jpg')


def photo_recipe(url):
    """Фото ЖК под квадратную плитку: обрезаем сами (rt:fill), а не отдаём это
    Директу. Полей не делаем — для фотографии кроп уместнее, чем белые полосы.
    Как и plan_recipe: дефолтный imgproxy только для api, иначе — url как есть."""
    tpl = _C.get('FEED_PHOTO_RECIPE', '').strip()
    if tpl:
        return tpl.format(url=url)
    if SOURCE != 'api':
        return url
    return f'{SITE}/proxy/insecure/w:1200/h:1200/rt:fill/q:85/plain/{url}@jpg'


def mirror(url):
    """Кладём обработанную картинку к себе и возвращаем ссылку на свой сервер.

    Две ветки, обе не меняют публичный URL — он считается от рецепта, а не от
    того, удалось ли скачать. Директ поэтому не видит наших сбоев:
      1. файла нет  -> качаем у обработчика;
      2. не отдал   -> оставляем то, что лежит со вчера (имя контентное, значит
                       это ровно та же картинка). Нет и вчерашней — пропускаем.
    """
    fname = hashlib.sha1(url.encode('utf-8')).hexdigest()[:16] + '.jpg'
    path = os.path.join(IMG_DIR, fname)
    public = f'{BASE_URL}/img/{fname}'
    USED.add(fname)

    if os.path.exists(path) and os.path.getsize(path) > 0:
        return public

    try:
        data = fetch(url)
    except Exception as exc:
        if os.path.exists(path):
            return public
        SKIPPED.append(f'{fname}: {exc}')
        return None

    tmp = path + '.tmp'
    with open(tmp, 'wb') as fh:
        fh.write(data)
    os.replace(tmp, path)
    time.sleep(REQUEST_PAUSE)   # не долбим обработчик пачкой
    return public


def image(url, recipe):
    """Единая точка: рецепт обработки один и тот же, отличается только то,
    кто раздаёт результат — мы или обработчик источника."""
    if not url:
        return None
    ready = recipe(url)
    return mirror(ready) if BASE_URL else ready


# ─── Нормализованная модель ──────────────────────────────────────────────────
# Проект: name, address, image, due, page, catalog, catalog_rooms.
# Лот: id, url, rooms, area (строка как в источнике), price, oldprice,
#      floor, floors_total, building, section, number, ppm, finishing,
#      features (список строк), promo, plan, plan2, due.
# Отсутствующее поле — None: тег/параметр тогда не выводится вообще
# (пустой тег Директ считает ошибкой).

def source_api():
    p = get(f'{API}/projects/{PROJECT}/')
    listing = get(f'{API}/properties/?limit=1000')
    ids = [r['id'] for r in listing['results']]
    if not ids:
        raise SystemExit('API вернул пустой список лотов — фид не перезаписываю')

    # Карточки забираем последовательно, с паузой: источник чужой, а прогон
    # раз в сутки — спешить некуда. Пик нагрузки ~2 rps вместо всплеска,
    # на который мог бы среагировать WAF.
    raw = []
    for n, i in enumerate(ids):
        if n:
            time.sleep(REQUEST_PAUSE)
        raw.append(get(f'{API}/properties/{i}/'))
    raw.sort(key=lambda f: f['id'])

    due = f'{p["completion_quarter"]} кв. {p["completion_year"]}'
    project = {
        'name': p['name'],
        'address': p.get('address'),
        'image': p.get('image'),
        'due': due,
        'page': f'{SITE}/projects/{PROJECT}',
        'catalog': f'{SITE}/flats?project={PROJECT}',
        'catalog_rooms': f'{SITE}/flats?project={PROJECT}&rooms={{rooms}}',
    }

    lots = []
    for f in raw:
        plan = f.get('plan') or {}
        b = f['building']
        lots.append({
            'id': f['id'],
            'url': f'{SITE}/projects/{f["project"]["slug"]}/flats/{f["id"]}',
            'rooms': f['rooms'],
            'area': f['area'],
            'price': float(f['price']),
            'oldprice': float(f['original_price']) if f.get('original_price') else None,
            'floor': f['floor']['number'],
            'floors_total': f['section']['floors_count'],
            'building': b['number'],
            'section': f['section']['number'],
            'number': f.get('number'),
            'ppm': int(float(f['price_per_meter'])) if f.get('price_per_meter') else None,
            'finishing': f.get('finishing'),
            'features': [x['name'] for x in f.get('features') or []],
            'promo': (f.get('discount_description') or f.get('discount_name'))
                     if f.get('discount_name') else None,
            'plan': plan.get('plan_with_furniture'),
            'plan2': plan.get('plan'),
            'due': f'{b["completion_quarter"]} кв. {b["completion_year"]}',
        })
    return project, lots


def _strip_ns(root):
    for el in root.iter():
        if '}' in el.tag:
            el.tag = el.tag.split('}', 1)[1]
    return root


def source_yrl():
    """Готовый YRL-фид застройщика (выгрузка для Циана/Я.Недвижимости).
    Конвертируем что есть; oldprice в YRL не существует — скидок не будет,
    пока источник их не отдаст иным способом."""
    root = _strip_ns(ET.fromstring(fetch(YRL_URL)))
    offers = root.findall('.//offer')
    if not offers:
        raise SystemExit('YRL-фид пуст — фид не перезаписываю')

    def txt(el, path):
        node = el.find(path)
        return node.text.strip() if node is not None and node.text else None

    lots = []
    for o in offers:
        rooms = txt(o, 'rooms')
        studio = (txt(o, 'studio') or '').lower() in ('да', 'true', '1', 'yes')
        images = [i.text.strip() for i in o.findall('image') if i.text]
        by, rq = txt(o, 'built-year'), txt(o, 'ready-quarter')
        lots.append({
            'id': o.get('internal-id'),
            'url': txt(o, 'url'),
            'rooms': 0 if studio else int(rooms or 0),
            'area': txt(o, 'area/value'),
            'price': float(txt(o, 'price/value') or 0),
            'oldprice': None,
            'floor': txt(o, 'floor'),
            'floors_total': txt(o, 'floors-total'),
            'building': None,   # в YRL номера корпуса нет; building-name = имя ЖК
            'section': txt(o, 'building-section'),
            'number': txt(o, 'apartments'),
            'ppm': None,
            'finishing': txt(o, 'renovation'),
            'features': [],
            'promo': None,
            'plan': images[0] if images else None,
            'plan2': None,
            'due': f'{rq} кв. {by}' if by and rq else None,
        })
    lots = [l for l in lots if l['id'] and l['url'] and l['price'] > 0 and l['area']]
    if not lots:
        raise SystemExit('в YRL-фиде нет пригодных лотов — фид не перезаписываю')
    lots.sort(key=lambda l: str(l['id']))

    first = offers[0]
    project = {
        'name': _C.get('FEED_NAME') or txt(first, 'building-name') or COMPANY or 'Каталог',
        'address': txt(first, 'location/address'),
        'image': None,
        'due': lots[0]['due'],
        'page': SITE or (lots[0]['url'].split('/', 3)[0] + '//' + lots[0]['url'].split('/', 3)[2]),
        'catalog': _C.get('FEED_CATALOG_URL') or None,
        'catalog_rooms': _C.get('FEED_CATALOG_ROOMS_URL') or None,
    }
    return project, lots


# Колонки CSV ищем по названиям. У поля несколько принятых синонимов —
# менеджеры называют столбцы по-разному. Сопоставление нечёткое: заголовок
# совпадает с синонимом целиком или начинается с него на границе слова
# («Цена, руб» → цена, «Цена за метр» → ppm, а не price — берём длиннейший).
FIELD_ALIASES = {
    # обязательные
    'id':           ['id', 'ид', 'идентификатор', 'артикул', 'код лота', 'код'],
    'url':          ['url', 'ссылка на лот', 'ссылка на страницу', 'ссылка', 'страница'],
    'rooms':        ['комнатность', 'количество комнат', 'кол-во комнат',
                     'комнат', 'комнаты', 'rooms'],
    'area':         ['общая площадь', 'площадь', 'метраж', 'area'],
    'price':        ['цена со скидкой', 'актуальная цена', 'цена', 'стоимость', 'price'],
    # необязательные
    'oldprice':     ['цена без скидки', 'цена до скидки', 'старая цена',
                     'старая стоимость', 'базовая цена', 'oldprice'],
    'floor':        ['этаж', 'floor'],
    'floors_total': ['этажей всего', 'всего этажей', 'этажность', 'этажей', 'floors'],
    'building':     ['корпус', 'литер', 'building'],
    'section':      ['секция', 'подъезд', 'section'],
    'number':       ['номер квартиры', 'квартира', 'apartments', 'apartment'],
    'ppm':          ['цена за квадратный метр', 'цена за метр', 'цена за м2',
                     'цена за кв.м', 'за метр', 'price per meter'],
    'finishing':    ['тип отделки', 'отделка', 'renovation'],
    'features':     ['удобства', 'особенности', 'характеристики', 'теги', 'features'],
    'promo':        ['спецпредложение', 'акция', 'скидка', 'промо', 'promo'],
    'plan':         ['ссылка на планировку', 'планировка', 'план', 'изображение',
                     'картинка', 'фото', 'image', 'plan'],
    'due':          ['ввод в эксплуатацию', 'срок сдачи', 'сдача', 'готовность',
                     'срок', 'due'],
    'address':      ['адрес', 'address'],
}
REQUIRED_CSV = ('id', 'url', 'rooms', 'area', 'price')


def _norm_head(h):
    return ' '.join((h or '').strip().lower().replace('ё', 'е').split())


def _match_columns(headers):
    """Заголовок -> поле. Длиннейший подходящий синоним выигрывает, поле
    занимает первый подходящий столбец (дубли игнорируются)."""
    pairs = sorted(((_norm_head(a), f) for f, al in FIELD_ALIASES.items() for a in al),
                   key=lambda p: -len(p[0]))
    colmap = {}
    for idx, raw in enumerate(headers):
        h = _norm_head(raw)
        if not h:
            continue
        for alias, field in pairs:
            if field in colmap:
                continue
            boundary = h == alias or (h.startswith(alias) and not h[len(alias)].isalnum())
            if boundary:
                colmap[field] = idx
                break
    return colmap


def _num(v):
    """'15 000 000 ₽' -> '15000000', '42,5 м²' -> '42.5'. None, если чисел нет."""
    if not v:
        return None
    v = v.replace('\xa0', '').replace(' ', '').replace(',', '.')
    m = re.search(r'-?\d+(?:\.\d+)?', v)
    return m.group() if m else None


def _rooms_val(v):
    v = (v or '').strip().lower()
    if not v or 'студ' in v or 'studio' in v:
        return 0
    m = re.search(r'\d+', v)
    return int(m.group()) if m else 0


def _columns_help():
    lines = ['  обязательные:']
    for f in REQUIRED_CSV:
        lines.append(f'    {f:13} ← ' + ', '.join(FIELD_ALIASES[f][:4]))
    lines.append('  необязательные (подтянутся, если есть):')
    for f in FIELD_ALIASES:
        if f not in REQUIRED_CSV:
            lines.append(f'    {f:13} ← ' + ', '.join(FIELD_ALIASES[f][:4]))
    return '\n'.join(lines)


def _read_csv_bytes():
    src = _C.get('FEED_CSV_PATH') or _C.get('FEED_CSV_URL')
    if _C.get('FEED_CSV_PATH'):
        path = _C['FEED_CSV_PATH']
        age_days = (time.time() - os.path.getmtime(path)) / 86400
        limit = float(_C.get('FEED_CSV_MAX_AGE_DAYS', 14))
        if age_days > limit:
            print(f'  ВНИМАНИЕ: {path} не обновлялся {age_days:.0f} дней '
                  f'(порог {limit:.0f}) — данные могут быть устаревшими', file=sys.stderr)
        return open(path, 'rb').read()
    return fetch(src)


def source_csv():
    """Таблица от клиента. Колонки — по названиям (см. FIELD_ALIASES).
    Кодировка: UTF-8 (в т.ч. с BOM) или CP1251. Разделитель определяется сам."""
    raw = _read_csv_bytes()
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw.decode('cp1251')
    sample = text[:4096]
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=',;\t').delimiter
    except csv.Error:
        delim = ';' if sample.count(';') >= sample.count(',') else ','

    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise SystemExit('CSV пуст или в нём только заголовок — фид не перезаписываю')

    colmap = _match_columns(rows[0])
    have_url = 'url' in colmap or _C.get('FEED_URL_TEMPLATE')
    missing = [f for f in REQUIRED_CSV if f not in colmap and not (f == 'url' and have_url)]
    if missing:
        raise SystemExit(
            'В CSV не нашлись обязательные колонки: ' + ', '.join(missing) + '.\n'
            'Заголовки в файле: ' + ', '.join(h for h in rows[0] if h.strip()) + '\n'
            'Как называть колонки:\n' + _columns_help() + '\n'
            '(если ссылок на лоты нет — задайте FEED_URL_TEMPLATE с {id}).')

    def cell(row, field):
        idx = colmap.get(field)
        return row[idx].strip() if idx is not None and idx < len(row) else ''

    url_tpl = _C.get('FEED_URL_TEMPLATE', '')
    lots = []
    for row in rows[1:]:
        lot_id = cell(row, 'id')
        price = _num(cell(row, 'price'))
        area = _num(cell(row, 'area'))
        if not (lot_id and price and area):
            continue
        url = cell(row, 'url') or (url_tpl.format(id=lot_id) if url_tpl else '')
        if not url:
            continue
        oldp = _num(cell(row, 'oldprice'))
        feats = [x.strip() for x in re.split(r'[;,|]', cell(row, 'features')) if x.strip()]
        lots.append({
            'id': lot_id,
            'url': url,
            'rooms': _rooms_val(cell(row, 'rooms')),
            'area': area,
            'price': float(price),
            'oldprice': float(oldp) if oldp else None,
            'floor': cell(row, 'floor') or None,
            'floors_total': cell(row, 'floors_total') or None,
            'building': cell(row, 'building') or None,
            'section': cell(row, 'section') or None,
            'number': cell(row, 'number') or None,
            'ppm': _num(cell(row, 'ppm')),
            'finishing': cell(row, 'finishing') or None,
            'features': feats,
            'promo': cell(row, 'promo') or None,
            'plan': cell(row, 'plan') or None,
            'plan2': None,
            'due': cell(row, 'due') or None,
        })
    if not lots:
        raise SystemExit('в CSV нет пригодных строк (нужны id, цена, площадь, ссылка) '
                         '— фид не перезаписываю')
    lots.sort(key=lambda l: str(l['id']))

    project = {
        'name': _C['FEED_NAME'],
        'address': _C.get('FEED_ADDRESS') or None,
        'image': None,
        'due': lots[0]['due'],
        'page': SITE or _C.get('FEED_CATALOG_URL') or lots[0]['url'],
        'catalog': _C.get('FEED_CATALOG_URL') or None,
        'catalog_rooms': _C.get('FEED_CATALOG_ROOMS_URL') or None,
    }
    return project, lots


SOURCES = {'api': source_api, 'yrl': source_yrl, 'csv': source_csv}


# ─── Сборка XML ──────────────────────────────────────────────────────────────

def kind(lot):
    return 'Студия' if lot['rooms'] == 0 else f'{lot["rooms"]}-комнатная квартира'


def cat_name(rooms):
    return 'Студии' if rooms == 0 else f'{rooms}-комнатные квартиры'


def price_bucket(lot):
    mln = lot['price'] / 1_000_000
    for edge, label in ((40, 'до 40 млн'), (60, '40–60 млн'), (80, '60–80 млн'),
                        (100, '80–100 млн')):
        if mln < edge:
            return label
    return 'от 100 млн'


def labels(lot):
    """custom_label_0..4 — единственные произвольные поля, по которым Директ
    умеет фильтровать в ЕПК. По <param> фильтры не строятся."""
    out = [('custom_label_0', kind(lot)),
           ('custom_label_4', price_bucket(lot))]
    if lot['building'] is not None:
        out.append(('custom_label_1', f'Корпус {lot["building"]}'))
    if lot['due']:
        out.append(('custom_label_3', lot['due']))
    if lot['finishing']:
        out.append(('custom_label_2', lot['finishing']))
    out.sort()
    return '\n        '.join(f'<{k}>{escape(v)}</{k}>' for k, v in out)


def name(lot, project):
    return f'{kind(lot)} {lot["area"]} м² в ЖК «{project["name"]}»'


def description(lot, project):
    parts = [f'{kind(lot)} {lot["area"]} м² в ЖК «{project["name"]}»']
    if lot['floor'] is not None and lot['floors_total'] is not None:
        parts.append(f'{lot["floor"]} этаж из {lot["floors_total"]}')
    if lot['building'] is not None and lot['section'] is not None:
        parts.append(f'корпус {lot["building"]}, секция {lot["section"]}')
    if lot['finishing']:
        parts.append(f'отделка: {lot["finishing"]}')
    if lot['features']:
        parts.append(', '.join(lot['features']))
    if lot['promo']:
        parts.append(lot['promo'])
    return '. '.join(parts) + '.'


def pictures(lot, project):
    urls = [image(lot['plan'], plan_recipe),
            image(lot['plan2'], plan_recipe),
            image(project.get('image'), photo_recipe)]
    urls = [u for u in dict.fromkeys(urls) if u]
    return '\n        '.join(f'<picture>{escape(u)}</picture>' for u in urls)


def params(lot, project):
    """Необязательные поля отдаём только когда они заполнены: пустой тег
    Директ считает ошибкой, а заглушка утекает в текст объявления."""
    out = [('Тип', kind(lot)),
           ('Комнат', str(lot['rooms'])),
           ('Площадь', lot['area'])]
    for label, key in (('Этаж', 'floor'), ('Этажей в секции', 'floors_total'),
                       ('Корпус', 'building'), ('Секция', 'section')):
        if lot[key] is not None:
            out.append((label, str(lot[key])))
    if lot['due']:
        out.append(('Срок сдачи', lot['due']))
    out.append(('ЖК', project['name']))
    if lot['finishing']:
        out.append(('Отделка', lot['finishing']))
    if lot['number']:
        out.append(('Номер квартиры', str(lot['number'])))
    if lot['ppm']:
        out.append(('Цена за м²', str(lot['ppm'])))
    return '\n        '.join(
        f'<param name={quoteattr(k)}>{escape(str(v))}</param>' for k, v in out)


def oldprice(lot):
    """Директ требует oldprice строго больше price, иначе оффер отклоняется."""
    if not lot['oldprice'] or lot['oldprice'] <= lot['price']:
        return ''
    return f'        <oldprice>{int(lot["oldprice"])}</oldprice>\n'


def typical_plan(same):
    """Планировка для плитки группы: берём самую растиражированную в группе —
    она и есть «типичная». При равенстве — лот с меньшим id, чтобы URL не скакал
    от прогона к прогону: смена ссылки заставляет Директ перекачивать картинку."""
    counts = {}
    for lot in sorted(same, key=lambda l: str(l['id'])):
        if lot['plan']:
            counts[lot['plan']] = counts.get(lot['plan'], 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda u: (counts[u], -list(counts).index(u)))


def collections(lots, project):
    """Страницы каталога. Без них Директ не даёт выбрать фид в кампаниях,
    где есть объявления для каталога. Если источник не знает адресов каталога
    (FEED_CATALOG_URL не задан) — блок не выводится вовсе."""
    if not project.get('catalog'):
        return ''
    render = image(project.get('image'), photo_recipe)
    due = f' Сдача — {project["due"]}.' if project.get('due') else ''

    # description коллекции идёт в текст объявления (name — в заголовок).
    # Лимит Директа: 81 символ и 15 знаков препинания. Цена — минимальная
    # по группе, без округления: округлять вниз нельзя (цена окажется ниже
    # реальной), вверх — «от» перестанет быть правдой. Счётчик лотов убран
    # сознательно: он меняется чаще всего и гоняет объявление по перемодерации.
    all_min = min(l['price'] for l in lots) / 1e6
    addr = f' {project["address"]}.' if project.get('address') else ''
    items = [('all', project['catalog'], f'Квартиры в ЖК «{project["name"]}»',
              f'От {all_min:.1f} млн ₽.{addr}{due}', render)]

    if project.get('catalog_rooms'):
        for r in sorted({l['rooms'] for l in lots}):
            same = [l for l in lots if l['rooms'] == r]
            grp_min = min(l['price'] for l in same) / 1e6
            area_min = min(float(l['area']) for l in same)
            plan = image(typical_plan(same), plan_recipe)
            items.append((
                f'rooms-{r}',
                project['catalog_rooms'].format(rooms=r),
                f'{cat_name(r)} в ЖК «{project["name"]}»',
                f'От {grp_min:.1f} млн ₽, площадь от {area_min:.1f} м².{due}',
                plan or render))

    out = []
    for cid, url, nm, desc, pic in items:
        pics = f'\n        <picture>{escape(pic)}</picture>' if pic else ''
        out.append(f'''      <collection id="{cid}">
        <url>{escape(url)}</url>{pics}
        <name>{escape(nm)}</name>
        <description>{escape(desc)}</description>
      </collection>''')
    return '\n'.join(out)


def sweep():
    """Убираем картинки, которые больше не нужны: лот ушёл из продажи или
    сменилась планировка. Делаем только после успешной сборки, иначе рискуем
    снести файлы из-за случайного сбоя."""
    if not (BASE_URL and os.path.isdir(IMG_DIR)):
        return 0
    gone = 0
    for fname in os.listdir(IMG_DIR):
        if fname.endswith('.jpg') and fname not in USED:
            os.remove(os.path.join(IMG_DIR, fname))
            gone += 1
    return gone


def build():
    project, lots = SOURCES[SOURCE]()

    if BASE_URL:
        os.makedirs(IMG_DIR, exist_ok=True)

    now = datetime.now(MSK).replace(microsecond=0)

    offers = []
    for lot in lots:
        cid_rooms = f'\n        <collectionId>rooms-{lot["rooms"]}</collectionId>' \
            if project.get('catalog_rooms') else ''
        cid_all = f'\n        <collectionId>all</collectionId>' \
            if project.get('catalog') else ''
        offers.append(f'''      <offer id="{lot['id']}" available="true">
        <name>{escape(name(lot, project))}</name>
        <url>{escape(lot['url'])}</url>
        <price>{int(lot['price'])}</price>
{oldprice(lot)}        <currencyId>RUR</currencyId>
        <categoryId>{100 + lot['rooms']}</categoryId>
        {pictures(lot, project)}
        <description>{escape(description(lot, project))}</description>{cid_all}{cid_rooms}
        {labels(lot)}
        {params(lot, project)}
      </offer>''')

    cats = '\n'.join(
        f'      <category id="{100 + r}" parentId="1">{cat_name(r)}</category>'
        for r in sorted({l['rooms'] for l in lots}))

    cols = collections(lots, project)
    cols_block = f'''
    <collections>
{cols}
    </collections>''' if cols else ''

    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<yml_catalog date="{now:%Y-%m-%d %H:%M}">
  <shop>
    <name>{escape(project['name'])}</name>
    <company>{escape(COMPANY or project['name'])}</company>
    <url>{escape(project['page'])}</url>
    <currencies>
      <currency id="RUR" rate="1"/>
    </currencies>
    <categories>
      <category id="1">Квартиры</category>
{cats}
    </categories>
    <offers>
{chr(10).join(offers)}
    </offers>{cols_block}
  </shop>
</yml_catalog>
'''
    n_old = sum(1 for l in lots if oldprice(l))
    return xml, len(lots), n_old


if __name__ == '__main__':
    xml, n, n_old = build()
    os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
    tmp = OUT + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(xml)
    os.replace(tmp, OUT)

    stamp = f'{datetime.now(MSK):%Y-%m-%d %H:%M:%S}'
    mode = f'зеркало ({len(USED)} картинок)' if BASE_URL else 'ссылки на обработчик источника'
    print(f'{stamp} — источник: {SOURCE}, офферов: {n}, со старой ценой: {n_old}, '
          f'картинки: {mode}, файл: {OUT}')
    if BASE_URL:
        gone = sweep()
        if gone:
            print(f'{stamp} — удалено неиспользуемых картинок: {gone}')
    if SKIPPED:
        print(f'{stamp} — не удалось получить {len(SKIPPED)} картинок:')
        for s in SKIPPED[:5]:
            print('   ', s)
