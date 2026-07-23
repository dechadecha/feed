#!/usr/bin/env bash
# Установка генератора фида с нуля. Запускать из папки репозитория:
#
#     sudo ./setup.sh                     # адрес определит сам
#     sudo ./setup.sh 203.0.113.10        # или задать вручную
#     sudo ./setup.sh feeds.example.com   # можно домен
#
# Ставит файлы, поднимает веб-сервер, заводит расписание, собирает фид сразу
# и печатает готовую ссылку для кабинета Директа. Повторный запуск безопасен.
set -euo pipefail

FEED_DIR=/var/www/html/feeds
APP_DIR=/opt/feed
UNIT=feed

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "нужны права root: sudo ./setup.sh"
[ -f build_feed.py ] || die "build_feed.py не найден — запускайте из папки репозитория"

# ─── 1. Куда будут смотреть ссылки ───────────────────────────────────────────
say "Определяю адрес сервера"
HOST="${1:-}"
if [ -z "$HOST" ]; then
    for svc in https://api.ipify.org https://ifconfig.me/ip; do
        HOST=$(curl -s --max-time 10 "$svc" 2>/dev/null || true)
        [ -n "$HOST" ] && break
    done
fi
[ -z "$HOST" ] && HOST=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$HOST" ] && die "не смог определить адрес — задайте вручную: sudo ./setup.sh <IP или домен>"

case "$HOST" in
    http://*|https://*) BASE="$HOST" ;;
    *)                  BASE="http://$HOST" ;;
esac
FEED_URL="$BASE/feeds/feed.xml"
echo "  адрес: $BASE"

# Если задали домен — убеждаемся, что он ведёт сюда. Иначе фид соберётся
# со ссылками на картинки, которых по этому адресу нет, и выяснится это
# только когда Директ придёт за ними.
NAME=${BASE#*://}; NAME=${NAME%%/*}; NAME=${NAME%%:*}
if ! printf '%s' "$NAME" | grep -qE '^[0-9]+(\.[0-9]+){3}$'; then
    RESOLVED=$(getent ahostsv4 "$NAME" 2>/dev/null | awk 'NR==1{print $1}')
    if [ -z "$RESOLVED" ]; then
        die "домен $NAME не резолвится. Проверьте A-запись или укажите IP:
     sudo bash setup.sh <IP>"
    fi
    MYIPS=$(hostname -I 2>/dev/null)
    PUBIP=$(curl -s --max-time 10 https://api.ipify.org 2>/dev/null || true)
    if ! printf '%s %s' "$MYIPS" "$PUBIP" | grep -qw "$RESOLVED"; then
        warn "домен ведёт на $RESOLVED, а машина отвечает с ${PUBIP:-неизвестно}"
        warn "если перед сервером CDN или прокси — это нормально, продолжаю"
    else
        echo "  домен резолвится сюда: $RESOLVED"
    fi
fi

# Предупреждаем о смене адреса: ссылки на картинки поменяются, и Директ
# будет выкачивать их заново — несколько часов заглушек в превью.
OLD=$(grep -hoP 'FEED_BASE_URL=\K\S+' $APP_DIR/feed.conf /etc/systemd/system/$UNIT.service 2>/dev/null | head -1 || true)
if [ -n "$OLD" ] && [ "$OLD" != "$BASE/feeds" ]; then
    warn "адрес меняется: $OLD -> $BASE/feeds"
    warn "Директ перекачает картинки, в превью несколько часов будут заглушки"
fi

# ─── 1.5. Источник данных ────────────────────────────────────────────────────
# Адреса API и сайта в коде и репозитории не хранятся. Берём в порядке
# приоритета: переменные окружения -> уже сохранённый feed.conf -> спросить.
say "Источник данных"
CONF="$APP_DIR/feed.conf"
if [ -z "${FEED_API:-}" ] && [ -f "$CONF" ]; then
    # shellcheck disable=SC1090
    . "$CONF"
    echo "  взял из $CONF"
fi
if [ -z "${FEED_API:-}" ] || [ -z "${FEED_SITE:-}" ] || [ -z "${FEED_PROJECT:-}" ]; then
    if [ -t 0 ]; then
        echo "  Три параметра источника — выдаёт владелец репозитория:"
        read -rp "  Адрес API (вида https://сайт/api): " FEED_API
        read -rp "  Адрес сайта (вида https://сайт):   " FEED_SITE
        read -rp "  Слаг проекта в API:                " FEED_PROJECT
        read -rp "  Компания в шапке фида (Enter — имя проекта): " FEED_COMPANY
    else
        die "источник не задан. Запустите интерактивно, либо так:
     FEED_API=... FEED_SITE=... FEED_PROJECT=... sudo -E bash setup.sh"
    fi
fi
FEED_API=${FEED_API%/}; FEED_SITE=${FEED_SITE%/}
[ -n "$FEED_API" ] && [ -n "$FEED_SITE" ] && [ -n "$FEED_PROJECT" ] \
    || die "параметры источника не могут быть пустыми"
echo "  API: $FEED_API | проект: $FEED_PROJECT"

# ─── 2. Зависимости ──────────────────────────────────────────────────────────
say "Проверяю окружение"
command -v python3 >/dev/null || die "нет python3 — поставьте: apt install python3"
echo "  python: $(python3 -V)"
python3 -c 'import urllib.request, json, hashlib' \
    || die "стандартная библиотека python неполная"

if ! command -v nginx >/dev/null; then
    warn "nginx не найден, ставлю"
    apt-get update -qq && apt-get install -y -qq nginx
fi
systemctl enable --now nginx >/dev/null 2>&1 || true
echo "  nginx: $(nginx -v 2>&1)"

say "Проверяю доступ к API источника"
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
       "$FEED_API/properties/?limit=1" || true)
[ "$CODE" = "200" ] || die "API отвечает $CODE вместо 200 — сервер не видит $FEED_API"
echo "  API: 200 OK"

# ─── 3. Файлы ────────────────────────────────────────────────────────────────
say "Раскладываю файлы"
install -d -m 755 "$APP_DIR" "$FEED_DIR" "$FEED_DIR/img"
install -m 755 build_feed.py "$APP_DIR/build_feed.py"
chown -R www-data:www-data "$FEED_DIR" 2>/dev/null || true

# Конфиг с источником и адресом — единственное место, где всё это записано.
# Генератор читает его сам (feed.conf рядом со скриптом), поэтому systemd,
# cron и запуск руками работают одинаково, без переменных окружения.
cat > "$CONF" <<EOF
FEED_API=$FEED_API
FEED_SITE=$FEED_SITE
FEED_PROJECT=$FEED_PROJECT
FEED_COMPANY=${FEED_COMPANY:-}
FEED_BASE_URL=$BASE/feeds
EOF
chmod 640 "$CONF"
echo "  $APP_DIR/build_feed.py"
echo "  $CONF"
echo "  $FEED_DIR/"

# ─── 4. Расписание ───────────────────────────────────────────────────────────
say "Настраиваю автозапуск"
if ! command -v systemctl >/dev/null; then
    warn "systemd нет, ставлю задание в cron"
    CRON="20 2 * * * /usr/bin/python3 $APP_DIR/build_feed.py $FEED_DIR/feed.xml >> /var/log/feed.log 2>&1"
    ( crontab -l 2>/dev/null | grep -v build_feed.py; echo "$CRON" ) | crontab -
    echo "  cron: ежедневно в 02:20 UTC"
    say "Первый прогон (~1.5 минуты)"
    python3 "$APP_DIR/build_feed.py" "$FEED_DIR/feed.xml"
else
    cat > /etc/systemd/system/$UNIT.service <<EOF
[Unit]
Description=Сборка YML-фида для Яндекс Директа
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $APP_DIR/build_feed.py $FEED_DIR/feed.xml
TimeoutStartSec=1800
# Разовый сбой сети не должен стоить суток: три попытки с паузой 10 минут.
Restart=on-failure
RestartSec=600
StartLimitBurst=3
EOF

    cat > /etc/systemd/system/$UNIT.timer <<EOF
[Unit]
Description=Ежедневное обновление фида

[Timer]
# 02:20 UTC = 05:20 МСК — до рабочего дня и до планового перезабора Директом
OnCalendar=*-*-* 02:20:00
Persistent=true
RandomizedDelaySec=900

[Install]
WantedBy=timers.target
EOF

    systemctl daemon-reload
    systemctl enable --now $UNIT.timer >/dev/null
    echo "  таймер: $(systemctl list-timers $UNIT.timer --no-pager | awk 'NR==2{print $1,$2,$3}')"

    say "Первый прогон (~1.5 минуты, ждём)"
    systemctl start $UNIT.service || true
    journalctl -u $UNIT -n 5 --no-pager -o cat | tail -3
    RESULT=$(systemctl show $UNIT.service -p Result --value)
    [ "$RESULT" = "success" ] || die "прогон завершился с Result=$RESULT, смотрите journalctl -u $UNIT"
fi

# ─── 5. Проверка ─────────────────────────────────────────────────────────────
say "Проверяю результат"
[ -s "$FEED_DIR/feed.xml" ] || die "фид не создан"
chown -R www-data:www-data "$FEED_DIR" 2>/dev/null || true

python3 - "$FEED_DIR/feed.xml" <<'PY'
import sys, xml.etree.ElementTree as ET
r = ET.parse(sys.argv[1]).getroot()
offers, cols = r.findall('.//offer'), r.findall('.//collection')
pics = {p.text for p in r.findall('.//picture')}
print(f'  офферов: {len(offers)}, страниц каталога: {len(cols)}, картинок: {len(pics)}')
assert offers and cols, 'фид собрался пустым'
PY

LOCAL=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1/feeds/feed.xml" || true)
if [ "$LOCAL" = "200" ]; then
    echo "  отдаётся веб-сервером: 200 OK"
else
    warn "локально отдаётся с кодом $LOCAL — проверьте, что nginx раздаёт /var/www/html"
fi

IMGS=$(find "$FEED_DIR/img" -name '*.jpg' | wc -l)
echo "  зеркало картинок: $IMGS файлов в $FEED_DIR/img"

cat <<EOF

────────────────────────────────────────────────────────────
 Готово.

 Ссылка для кабинета Директа:

   $FEED_URL

 В кабинете: Библиотека → Фиды → Добавить фид.
 Тип бизнеса — «Другой бизнес», формат YML (НЕ «Недвижимость»).

 Дальше фид обновляется сам каждое утро в 05:20 МСК.
 Картинки в превью появятся через несколько часов — Директ
 выкачивает их отдельно, уже после того как прочитал XML.

 Полезное:
   systemctl start $UNIT           пересобрать сейчас
   journalctl -u $UNIT -n 20       лог прогонов
   systemctl list-timers $UNIT.timer   когда следующий запуск
────────────────────────────────────────────────────────────
EOF
