#!/bin/sh
# SMS gateway installer for Keenetic / Netcraze routers with Entware (SMS-1).
#
#   curl -fsSL https://raw.githubusercontent.com/x-happy-x/sms-gateway/master/install.sh | sh
#
# Installs Python from Entware, the gateway into /opt/sms-gateway and its init
# script, asks for the RouterOS address and account the LTE modem is reached
# through, starts it and checks /api/health. Run it again to update: config.json
# and the SMS archive stay. Options:
#
#   --yes            take the defaults, ask nothing (config.json must exist)
#   --version=REF    tag or branch to install (default: master)
#   --from-dir=DIR   install from a local copy of the repository (testing)
#   --dry-run        show what would be done
#   --uninstall      stop the gateway and remove its init script (data stays)
#
# The RouterOS password is typed by you on the router; it goes only into
# /opt/sms-gateway/config.json (mode 600).
set -eu

REPO="${SMSGW_REPO:-x-happy-x/sms-gateway}"
APP=/opt/sms-gateway
INIT=/opt/etc/init.d/S99sms-gateway
FILES="server.py store.py gateway.py pdu.py routeros.py contacts.py S99sms-gateway"

YES=0 DRY=0 UNINSTALL=0 REF="master" FROM_DIR=""
for arg in "$@"; do
  case "$arg" in
    --yes|-y) YES=1 ;;
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --version=*) REF="${arg#*=}" ;;
    --from-dir=*) FROM_DIR="${arg#*=}" ;;
    --help|-h) sed -n '2,19p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "Неизвестный параметр: $arg (см. --help)" >&2; exit 2 ;;
  esac
done

if [ -t 1 ]; then B="$(printf '\033[1m')" G="$(printf '\033[32m')" Y="$(printf '\033[33m')" R="$(printf '\033[31m')" N="$(printf '\033[0m')"; else B="" G="" Y="" R="" N=""; fi
say() { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s%s\n' "$G" "$N" "$B" "$*" "$N"; }
warn() { printf '%s!%s %s\n' "$Y" "$N" "$*" >&2; }
die() { printf '%sОшибка:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
run() {
  if [ "$DRY" = 1 ]; then printf '   [dry-run] %s\n' "$*"; return 0; fi
  "$@"
}
has() { command -v "$1" >/dev/null 2>&1; }

TTY=""
if [ -r /dev/tty ] && [ -w /dev/tty ] && (: </dev/tty) 2>/dev/null; then TTY=/dev/tty; fi

ask() {
  if [ "$YES" = 1 ] || [ -z "$TTY" ]; then printf '%s' "$2"; return; fi
  printf '%s%s%s [%s]: ' "$B" "$1" "$N" "$2" >"$TTY"
  IFS= read -r ans <"$TTY" || ans=""
  if [ -n "$ans" ]; then printf '%s' "$ans"; else printf '%s' "$2"; fi
}

# ask_secret "question" → typed without echo
ask_secret() {
  [ -n "$TTY" ] || die "нужен терминал, чтобы ввести пароль"
  printf '%s%s%s: ' "$B" "$1" "$N" >"$TTY"
  stty -echo <"$TTY" 2>/dev/null || true
  IFS= read -r secret <"$TTY" || secret=""
  stty echo <"$TTY" 2>/dev/null || true
  printf '\n' >"$TTY"
  printf '%s' "$secret"
}

[ "$(id -u)" = 0 ] || die "запустите от root (SSH роутера)"
[ -x /opt/bin/opkg ] || die "нужен Entware в /opt"

say "${B}SMS-шлюз — установка на роутер${N}"
if [ -f "$APP/server.py" ]; then say "  установлен в $APP — обновление, config.json и архив сохранятся"; else say "  новая установка в $APP"; fi
[ "$DRY" = 1 ] && say "  режим: пробный (--dry-run), ничего не меняется"

if [ "$UNINSTALL" = 1 ]; then
  step "Останавливаю и убираю init-скрипт"
  if [ -e "$INIT" ]; then run sh "$INIT" stop || true; fi
  run rm -f "$INIT"
  say "Данные остались в $APP (config.json, archive.sqlite). Удалить полностью: rm -rf $APP"
  exit 0
fi

# ---------- Python ----------
if ! { has python3 || [ -x /opt/bin/python3 ]; } || ! /opt/bin/python3 -c 'import sqlite3, json' 2>/dev/null; then
  step "Python 3 из Entware"
  run opkg update
  run opkg install python3
  if [ "$DRY" = 0 ] && ! /opt/bin/python3 -c 'import sqlite3' 2>/dev/null; then run opkg install python3-sqlite3; fi
fi
if ! has curl && [ -z "$FROM_DIR" ]; then run opkg install curl ca-bundle; fi

# ---------- sources ----------
tmp="$(mktemp -d /opt/tmp/smsgw-install.XXXXXX 2>/dev/null || mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if [ -n "$FROM_DIR" ]; then
  src="$FROM_DIR"
else
  case "$REF" in v[0-9]*) url="https://github.com/$REPO/archive/refs/tags/$REF.tar.gz" ;; *) url="https://github.com/$REPO/archive/refs/heads/$REF.tar.gz" ;; esac
  step "Скачиваю $REPO ($REF)"
  if [ "$DRY" = 1 ]; then say "   [dry-run] $url"; src="$tmp/none"; else
    curl -fsSL --retry 3 --connect-timeout 15 -o "$tmp/src.tar.gz" "$url" || die "не удалось скачать $url"
    tar -xzf "$tmp/src.tar.gz" -C "$tmp" || die "архив не распаковался"
    src="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -1)"
  fi
fi
if [ "$DRY" = 0 ]; then
  for f in $FILES static/index.html; do [ -f "$src/$f" ] || die "в исходниках нет $f"; done
fi

# ---------- install ----------
if [ -f "$APP/server.py" ]; then
  backup="$APP/backup-$(date +%Y%m%d-%H%M%S)"
  step "Копия текущей версии → $backup"
  run mkdir -p "$backup"
  for f in $FILES; do if [ -f "$APP/$f" ]; then run cp -p "$APP/$f" "$backup/"; fi; done
  if [ -d "$APP/static" ]; then run cp -a "$APP/static" "$backup/static"; fi
fi

step "Устанавливаю в $APP"
run mkdir -p "$APP" /opt/var/run /opt/var/log
for f in $FILES; do run cp "$src/$f" "$APP/$f"; done
run chmod 755 "$APP/S99sms-gateway"
run rm -rf "$APP/static.new"
run cp -a "$src/static" "$APP/static.new"
run rm -rf "$APP/static"
run mv "$APP/static.new" "$APP/static"
run rm -rf "$APP/__pycache__"

# ---------- config.json ----------
if [ ! -f "$APP/config.json" ]; then
  [ "$YES" = 0 ] && [ -n "$TTY" ] || die "нет $APP/config.json: запустите установщик в терминале, он спросит настройки"
  say ""
  say "Шлюз читает и отправляет SMS через LTE-модем MikroTik по API RouterOS (порт 8728)."
  gw="$(ip route 2>/dev/null | awk '/^default/ {print $3; exit}')"
  lan="$(ip -4 -o addr show dev br0 2>/dev/null | awk '{print $4}' | head -1)"
  ros_host="$(ask "Адрес RouterOS" "${gw:-192.168.188.1}")"
  ros_user="$(ask "Пользователь RouterOS (лучше отдельный, с правами api, read, write)" "smsgw")"
  ros_pass="$(ask_secret "Пароль этого пользователя (не отображается)")"
  listen="$(ask "Адрес веб-интерфейса шлюза" "${lan%/*}")"
  port="$(ask "Порт веб-интерфейса" "8099")"
  if [ "$DRY" = 1 ]; then say "   [dry-run] записать $APP/config.json (600)"; else
    umask 077
    ROS_HOST="$ros_host" ROS_USER="$ros_user" ROS_PASS="$ros_pass" LISTEN="$listen" PORT="$port" OUT="$APP/config.json" /opt/bin/python3 - <<'PY'
import json, os
cfg = {
    "router": {"host": os.environ["ROS_HOST"], "user": os.environ["ROS_USER"], "password": os.environ["ROS_PASS"]},
    "listen": os.environ["LISTEN"],
    "port": int(os.environ["PORT"]),
    "poll_seconds": 30,
    "api_tokens": [],
}
tmp = os.environ["OUT"] + ".new"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, os.environ["OUT"])
PY
  fi
  ros_pass=""
fi

# ---------- service ----------
step "Запускаю"
run ln -sf "$APP/S99sms-gateway" "$INIT"
if [ "$DRY" = 0 ]; then
  sh "$INIT" stop >/dev/null 2>&1 || true
  sh "$INIT" start || die "шлюз не запустился — смотрите /opt/var/log/sms-gateway.log"
  addr="$(/opt/bin/python3 -c 'import json; c=json.load(open("/opt/sms-gateway/config.json")); print("%s:%s" % (c.get("listen","192.168.1.1"), c.get("port",8099)))')"
  i=0
  until curl -fsS -o /dev/null --max-time 3 "http://$addr/api/health" 2>/dev/null; do
    i=$((i + 1)); [ "$i" -ge 15 ] && die "шлюз не ответил на http://$addr/api/health — смотрите /opt/var/log/sms-gateway.log"
    sleep 1
  done
  say "${G}SMS-шлюз работает:${N} http://$addr/"
else
  run sh "$INIT" restart
fi
