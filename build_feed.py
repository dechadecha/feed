# -*- coding: utf-8 -*-
"""Собирает YML-фид для Яндекс Директа из открытого API сайта-источника.

YML (а не YRL) — потому что только в нём есть <oldprice>: Директ рисует по нему
шильдик со скидкой и показывает обе цены. В YRL старой цены нет вообще.

Источник данных в коде не прописан. Он задаётся конфигом feed.conf рядом со
скриптом (его создаёт setup.sh, спросив значения при установке) либо
переменными окружения — они имеют приоритет:

  FEED_API       адрес API, например https://example.ru/api
  FEED_SITE      адрес сайта, например https://example.ru
  FEED_PROJECT   слаг проекта в API
  FEED_BASE_URL  публичный адрес папки с фидом; пусто — картинки не зеркалируем,
                 ссылки ведут на imgproxy сайта-источника

Запускается по таймеру, пишет XML атомарно (tmp + rename), чтобы Директ
никогда не забрал наполовину записанный файл.

    python3 build_feed.py /var/www/html/feeds/feed.xml
"""
import hashlib
import json
import os
import sys
import time
import urllib.request
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
API = _C.get('FEED_API', '').rstrip('/')
SITE = _C.get('FEED_SITE', '').rstrip('/')
PROJECT = _C.get('FEED_PROJECT', '')
if not (API and SITE and PROJECT):
    raise SystemExit('Источник не задан: нужны FEED_API, FEED_SITE, FEED_PROJECT.\n'
                     'Запустите setup.sh — он спросит значения и сохранит feed.conf,\n'
                     'либо создайте feed.conf рядом со скриптом сами.')

COMPANY = _C.get('FEED_COMPANY', '')   # имя компании в шапке фида; пусто — имя проекта

OUT = sys.argv[1] if len(sys.argv) > 1 else 'feed.xml'
MSK = timezone(timedelta(hours=3))
REQUEST_PAUSE = 0.5  # секунд между запросами к API источника

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


def kind(f):
    return 'Студия' if f['rooms'] == 0 else f'{f["rooms"]}-комнатная квартира'


def cat_name(rooms):
    return 'Студии' if rooms == 0 else f'{rooms}-комнатные квартиры'


def price_bucket(f):
    mln = float(f['price']) / 1_000_000
    for edge, label in ((40, 'до 40 млн'), (60, '40–60 млн'), (80, '60–80 млн'),
                        (100, '80–100 млн')):
        if mln < edge:
            return label
    return 'от 100 млн'


def labels(f):
    """custom_label_0..4 — единственные произвольные поля, по которым Директ
    умеет фильтровать в ЕПК. По <param> фильтры не строятся."""
    out = [('custom_label_0', kind(f)),
           ('custom_label_1', f'Корпус {f["building"]["number"]}'),
           ('custom_label_3', f'{f["building"]["completion_quarter"]} кв. {f["building"]["completion_year"]}'),
           ('custom_label_4', price_bucket(f))]
    if f.get('finishing'):
        out.append(('custom_label_2', f['finishing']))
    out.sort()
    return '\n        '.join(f'<{k}>{escape(v)}</{k}>' for k, v in out)


def name(f, project):
    return f'{kind(f)} {f["area"]} м² в ЖК «{project["name"]}»'


def description(f, project):
    parts = [f'{kind(f)} {f["area"]} м² в ЖК «{project["name"]}»',
             f'{f["floor"]["number"]} этаж из {f["section"]["floors_count"]}',
             f'корпус {f["building"]["number"]}, секция {f["section"]["number"]}']
    if f.get('finishing'):
        parts.append(f'отделка: {f["finishing"]}')
    if f.get('features'):
        parts.append(', '.join(x['name'] for x in f['features']))
    if f.get('discount_name'):
        parts.append(f['discount_description'] or f['discount_name'])
    return '. '.join(parts) + '.'


# Директ обрезает картинку под формат блока и не даёт этим управлять из фида,
# поэтому вокруг планировки нужны поля — иначе кроп режет сам план.
#
# Планировки почти все вертикальные (53 из 67, медиана ш/в 0.68), и квадратное
# окно им не подходит: вписанный в квадрат вертикальный план занимает по ширине
# всего ~38% канвы и выглядит мелким. Поэтому окно прямоугольное, по фактическим
# пропорциям планировок, а до квадрата канва добирается асимметричными полями.
#
# Границы окна: ширина 56% канвы — столько оставляет центральный кроп 9:16,
# высота 86% — вертикальные и квадратные блоки её не трогают.
CANVAS = 1166           # сторона готовой картинки
PLAN_W = 650            # 56% — предел для вертикального кропа
PLAN_H = 1000           # 86% — предел для квадратного блока
PAD_V = (CANVAS - PLAN_H) // 2
PAD_H = (CANVAS - PLAN_W) // 2


def plan_recipe(url):
    """Планировки в хранилище — PNG с прозрачным фоном; Директ такое показывает
    непредсказуемо. Гоним через imgproxy сайта: rt:fit + ex — вписать в окно,
    pd — поля до квадрата, @jpg — убрать альфа-канал и подложить белый."""
    return (f'{SITE}/proxy/insecure/w:{PLAN_W}/h:{PLAN_H}/rt:fit/ex:1'
            f'/pd:{PAD_V}:{PAD_H}/q:85/plain/{url}@jpg')


def photo_recipe(url):
    """Фото ЖК под квадратную плитку каталога: обрезаем сами (rt:fill), а не
    отдаём это Директу, — так контролируем, что попадёт в кадр. Полей не делаем,
    для фотографии они не нужны, в отличие от планировки."""
    return f'{SITE}/proxy/insecure/w:1200/h:1200/rt:fill/q:85/plain/{url}@jpg'


def mirror(url):
    """Кладём обработанную картинку к себе и возвращаем ссылку на свой сервер.

    Две ветки, обе не меняют публичный URL — он считается от рецепта, а не от
    того, удалось ли скачать. Директ поэтому не видит наших сбоев:
      1. файла нет  -> качаем у imgproxy клиента;
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
    time.sleep(REQUEST_PAUSE)   # не долбим прокси клиента пачкой
    return public


def image(url, recipe):
    """Единая точка: рецепт обработки один и тот же, отличается только то,
    кто раздаёт результат — мы или сайт клиента."""
    if not url:
        return None
    ready = recipe(url)
    return mirror(ready) if BASE_URL else ready


def pictures(f, project):
    plan = (f.get('plan') or {})
    urls = [image(plan.get('plan_with_furniture'), plan_recipe),
            image(plan.get('plan'), plan_recipe),
            image(project.get('image'), photo_recipe)]
    urls = [u for u in dict.fromkeys(urls) if u]
    return '\n        '.join(f'<picture>{escape(u)}</picture>' for u in urls)


def params(f, project):
    """Отделку и прочие необязательные поля отдаём только когда они заполнены."""
    out = [('Тип', kind(f)),
           ('Комнат', '0' if f['rooms'] == 0 else str(f['rooms'])),
           ('Площадь', f['area']),
           ('Этаж', str(f['floor']['number'])),
           ('Этажей в секции', str(f['section']['floors_count'])),
           ('Корпус', str(f['building']['number'])),
           ('Секция', str(f['section']['number'])),
           ('Срок сдачи', f'{f["building"]["completion_quarter"]} кв. {f["building"]["completion_year"]}'),
           ('ЖК', project['name'])]
    if f.get('finishing'):
        out.append(('Отделка', f['finishing']))
    if f.get('number'):
        out.append(('Номер квартиры', str(f['number'])))
    if f.get('price_per_meter'):
        out.append(('Цена за м²', str(int(float(f['price_per_meter'])))))
    return '\n        '.join(
        f'<param name={quoteattr(k)}>{escape(str(v))}</param>' for k, v in out)


def oldprice(f):
    """Директ требует oldprice строго больше price, иначе оффер отклоняется."""
    cur, old = float(f['price']), float(f.get('original_price') or 0)
    if old <= cur:
        return ''
    return f'        <oldprice>{int(old)}</oldprice>\n'


def typical_plan(same):
    """Планировка для плитки группы: берём самую растиражированную в группе —
    она и есть «типичная». При равенстве — лот с меньшим id, чтобы URL не скакал
    от прогона к прогону: смена ссылки заставляет Директ перекачивать картинку."""
    counts = {}
    for f in sorted(same, key=lambda f: f['id']):
        u = (f.get('plan') or {}).get('plan_with_furniture')
        if u:
            counts[u] = counts.get(u, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda u: (counts[u], -list(counts).index(u)))


def collections(flats, project):
    """Страницы каталога. Без них Директ не даёт выбрать фид в кампаниях,
    где есть объявления для каталога. Ведут на страницу подбора с фильтром —
    параметр rooms сайт понимает (проверено: rooms=0 отдаёт ровно студии)."""
    base = f'{SITE}/flats?project={PROJECT}'
    render = image(project.get('image'), photo_recipe)

    # Для проекта целиком уместен рендер ЖК, для групп по комнатности —
    # планировка: так плитки каталога различимы между собой, а не пять копий.
    items = [('all', base, f'Квартиры в ЖК «{project["name"]}»',
              f'{len(flats)} квартир в ЖК «{project["name"]}» — {project["address"]}. '
              f'Срок сдачи: {project["completion_quarter"]} кв. {project["completion_year"]}.',
              render)]

    for r in sorted({f['rooms'] for f in flats}):
        same = [f for f in flats if f['rooms'] == r]
        prices = [float(f['price']) for f in same]
        plan = image(typical_plan(same), plan_recipe)
        items.append((
            f'rooms-{r}',
            f'{base}&rooms={r}',
            f'{cat_name(r)} в ЖК «{project["name"]}»',
            f'{len(same)} лотов, от {min(prices) / 1e6:.1f} млн ₽, '
            f'площадь от {min(float(f["area"]) for f in same):.1f} м².',
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
    project = get(f'{API}/projects/{PROJECT}/')
    listing = get(f'{API}/properties/?limit=1000')
    ids = [r['id'] for r in listing['results']]
    if not ids:
        raise SystemExit('API вернул пустой список квартир — фид не перезаписываю')

    # Забираем карточки последовательно, с паузой: API клиента чужой, а прогон
    # раз в сутки — спешить некуда. Так пик нагрузки на их сервер около 2 rps
    # вместо всплеска в 166 запросов за полминуты, на что мог бы среагировать WAF.
    flats = []
    for n, i in enumerate(ids):
        if n:
            time.sleep(REQUEST_PAUSE)
        flats.append(get(f'{API}/properties/{i}/'))
    flats.sort(key=lambda f: f['id'])

    if BASE_URL:
        os.makedirs(IMG_DIR, exist_ok=True)

    now = datetime.now(MSK).replace(microsecond=0)

    offers = []
    for f in flats:
        offers.append(f'''      <offer id="{f['id']}" available="true">
        <name>{escape(name(f, project))}</name>
        <url>{SITE}/projects/{f['project']['slug']}/flats/{f['id']}</url>
        <price>{int(float(f['price']))}</price>
{oldprice(f)}        <currencyId>RUR</currencyId>
        <categoryId>{100 + f['rooms']}</categoryId>
        {pictures(f, project)}
        <description>{escape(description(f, project))}</description>
        <collectionId>all</collectionId>
        <collectionId>rooms-{f['rooms']}</collectionId>
        {labels(f)}
        {params(f, project)}
      </offer>''')

    cats = '\n'.join(
        f'      <category id="{100 + r}" parentId="1">{cat_name(r)}</category>'
        for r in sorted({f['rooms'] for f in flats}))

    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<yml_catalog date="{now:%Y-%m-%d %H:%M}">
  <shop>
    <name>{escape(project['name'])}</name>
    <company>{escape(COMPANY or project['name'])}</company>
    <url>{SITE}/projects/{PROJECT}</url>
    <currencies>
      <currency id="RUR" rate="1"/>
    </currencies>
    <categories>
      <category id="1">Квартиры</category>
{cats}
    </categories>
    <offers>
{chr(10).join(offers)}
    </offers>
    <collections>
{collections(flats, project)}
    </collections>
  </shop>
</yml_catalog>
'''
    n_old = sum(1 for f in flats if oldprice(f))
    return xml, len(offers), n_old


if __name__ == '__main__':
    xml, n, n_old = build()
    os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
    tmp = OUT + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(xml)
    os.replace(tmp, OUT)

    stamp = f'{datetime.now(MSK):%Y-%m-%d %H:%M:%S}'
    mode = f'зеркало ({len(USED)} картинок)' if BASE_URL else 'ссылки на imgproxy клиента'
    print(f'{stamp} — офферов: {n}, из них со старой ценой: {n_old}, '
          f'картинки: {mode}, файл: {OUT}')
    if BASE_URL:
        gone = sweep()
        if gone:
            print(f'{stamp} — удалено неиспользуемых картинок: {gone}')
    if SKIPPED:
        print(f'{stamp} — не удалось получить {len(SKIPPED)} картинок:')
        for s in SKIPPED[:5]:
            print('   ', s)
