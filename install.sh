#!/usr/bin/env bash
# Установка бота автопостинга на Ubuntu/Debian — рядом с другими ботами, ничего у них не трогая.
#
#   sudo bash install.sh                        установить (спросит токен бота и ID админов)
#   sudo bash install.sh --reconfigure          поменять токен или админов
#   sudo bash install.sh --update               обновить код из GitHub и перезапустить
#   sudo bash install.sh --update-zip АРХИВ.zip обновить из ZIP-архива (если сервер не видит GitHub)
#   sudo bash install.sh --uninstall            удалить службу (папка с базой остаётся)
#
# Что создаётся: своё окружение Python в папке бота (.venv), системный пользователь «autopost»
# и служба systemd «autopost-bot». Порты не открываются, системный Python и чужие файлы не меняются.
set -Eeuo pipefail

SERVICE="autopost-bot"
SERVICE_USER="autopost"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ENV_FILE="$APP_DIR/.env"
VENV="$APP_DIR/.venv"
UNIT_FILE="/etc/systemd/system/$SERVICE.service"
MARKER="$APP_DIR/.autopost-install"
TOKEN_RE='^[0-9]{5,16}:[A-Za-z0-9_-]{30,}$'
ID_RE='^[0-9]{5,15}$'

MODE="install"
NO_SERVICE=0
ZIP_PATH=""
TOKEN=""
ADMIN_IDS=""
BOT_USERNAME=""
RUN_AS="$SERVICE_USER"
PYTHON=""

if [ -t 1 ]; then
    BOLD=$'\e[1m' GREEN=$'\e[32m' YELLOW=$'\e[33m' RED=$'\e[31m' RESET=$'\e[0m'
else
    BOLD="" GREEN="" YELLOW="" RED="" RESET=""
fi

say() { printf '%s\n' "$*"; }
info() { printf '%s➜%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s⚠ %s%s\n' "$YELLOW" "$*" "$RESET" >&2; }
die() {
    printf '%s✖ %s%s\n' "$RED" "$*" "$RESET" >&2
    exit 1
}

# ask ПРИГЛАШЕНИЕ — читает строку в REPLY; конец ввода = прерывание установки
ask() {
    REPLY=""
    if ! IFS= read -r -p "$1" REPLY; then
        say ""
        die "Ввод прерван — установка остановлена."
    fi
}

confirm() { # confirm "Вопрос" [y|n]
    local default="${2:-y}" hint="[Д/н]"
    [ "$default" = "y" ] || hint="[д/Н]"
    ask "$1 $hint "
    local answer="${REPLY:-$default}"
    # Точные строки, без [..]: в локали C кириллица сравнивалась бы по байтам
    case "$answer" in
        y | Y | yes | Yes | YES | д | Д | да | Да | ДА) return 0 ;;
        *) return 1 ;;
    esac
}

env_get() { # значение ключа из .env (пусто, если нет)
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

# ------------------------------------------------------------------ проверка токена

# 0 — токен рабочий, 1 — Telegram его не принял, 2 — токен уже использует другой запущенный бот,
# 3 — нет связи с api.telegram.org
check_token() {
    local token="$1" response code body
    response="$(curl -sS --max-time 15 -w '\n%{http_code}' "https://api.telegram.org/bot$token/getMe" 2>/dev/null)" || return 3
    code="${response##*$'\n'}"
    body="${response%$'\n'*}"
    case "$code" in
        200) BOT_USERNAME="$(printf '%s' "$body" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')" ;;
        401 | 404) return 1 ;;
        *) return 3 ;;
    esac
    # getUpdates без offset ничего не подтверждает и не «съедает» — только проверяет, что токен свободен.
    # 409 значит, что этот токен уже опрашивает другой процесс (или у бота включён вебхук).
    response="$(curl -sS --max-time 15 -w '\n%{http_code}' "https://api.telegram.org/bot$token/getUpdates?limit=1&timeout=0" 2>/dev/null)" || return 0
    [ "${response##*$'\n'}" = "409" ] && return 2
    return 0
}

ask_token() {
    local current="$1" input rc
    say ""
    say "${BOLD}Токен бота${RESET} — его выдаёт @BotFather (команда /newbot)."
    say "Нужен НОВЫЙ бот: токен уже работающего бота (например, tg link bot22) использовать нельзя."
    while true; do
        ask "Токен: "
        input="$(printf '%s' "$REPLY" | tr -d '[:space:]')"
        if [[ ! "$input" =~ $TOKEN_RE ]]; then
            warn "Не похоже на токен. Он выглядит так: 123456789:AAH4k... (цифры, двоеточие и ~35 символов)."
            continue
        fi
        rc=0
        check_token "$input" || rc=$?
        if [ "$rc" -eq 2 ] && [ "$input" = "$current" ]; then
            rc=0 # это наш же работающий бот
        fi
        case "$rc" in
            0)
                TOKEN="$input"
                info "Токен подходит: бот @${BOT_USERNAME:-?}"
                return
                ;;
            1) warn "Telegram не принял этот токен. Скопируйте его из @BotFather целиком." ;;
            2)
                warn "Этот токен уже использует другой запущенный бот (или у него включён вебхук)."
                warn "Два бота на одном токене мешают друг другу — создайте для автопостинга отдельного бота в @BotFather."
                ;;
            *)
                warn "Не удалось связаться с api.telegram.org — проверить токен не получилось."
                if confirm "Всё равно использовать этот токен?" n; then
                    TOKEN="$input"
                    return
                fi
                ;;
        esac
    done
}

ask_admins() {
    local ids=() n=1
    say ""
    say "${BOLD}Админы бота${RESET} — Telegram ID людей, которые смогут им управлять."
    say "Свой ID можно узнать у @userinfobot. Каждый админ должен потом открыть бота и нажать /start."
    while true; do
        if [ "$n" -eq 1 ]; then
            ask "ID админа №1: "
        else
            ask "ID админа №$n (Enter — готово): "
        fi
        local input
        input="$(printf '%s' "$REPLY" | tr -d '[:space:]')"
        if [ -z "$input" ]; then
            [ "${#ids[@]}" -gt 0 ] && break
            warn "Нужен хотя бы один админ."
            continue
        fi
        if [[ ! "$input" =~ $ID_RE ]]; then
            warn "ID — это только цифры, например 123456789."
            continue
        fi
        ids+=("$input")
        n=$((n + 1))
    done
    ADMIN_IDS="$(
        IFS=,
        printf '%s' "${ids[*]}"
    )"
    info "Админы: ${ADMIN_IDS//,/, }"
}

configure() {
    local current_token current_admins
    current_token="$(env_get BOT_TOKEN)"
    current_admins="$(env_get ADMIN_IDS)"
    [ -n "$current_admins" ] || current_admins="$(env_get ADMIN_ID)"

    # Без вопросов: AUTOPOST_BOT_TOKEN и AUTOPOST_ADMIN_IDS из окружения (для автоматической установки).
    # Имена с префиксом, чтобы случайно не подхватить BOT_TOKEN другого бота на этом сервере.
    if [ -n "${AUTOPOST_BOT_TOKEN:-}" ] && [ -n "${AUTOPOST_ADMIN_IDS:-}" ]; then
        TOKEN="$AUTOPOST_BOT_TOKEN"
        ADMIN_IDS="$(printf '%s' "$AUTOPOST_ADMIN_IDS" | tr -s ' ;' ',,')"
        [[ "$TOKEN" =~ $TOKEN_RE ]] || die "AUTOPOST_BOT_TOKEN не похож на токен бота."
        [[ "$ADMIN_IDS" =~ ^[0-9]{5,15}(,[0-9]{5,15})*$ ]] || die "AUTOPOST_ADMIN_IDS — ID админов через запятую."
        return
    fi

    if [ "$MODE" != "reconfigure" ] && [ -n "$current_token" ] && [ -n "$current_admins" ]; then
        if [ "$MODE" = "update" ]; then
            TOKEN="$current_token"
            ADMIN_IDS="$current_admins"
            return
        fi
        say ""
        say "Найдены настройки: токен ${current_token:0:12}…, админы ${current_admins//,/, }."
        if confirm "Оставить их?" y; then
            TOKEN="$current_token"
            ADMIN_IDS="$current_admins"
            return
        fi
    fi
    ask_token "$current_token"
    ask_admins
}

write_env() {
    local timezone log_level tmp
    timezone="$(env_get TIMEZONE)"
    log_level="$(env_get LOG_LEVEL)"
    tmp="$(mktemp "$APP_DIR/.env.XXXXXX")"
    cat >"$tmp" <<EOF
# Настройки бота. Изменить токен или админов: sudo bash install.sh --reconfigure
BOT_TOKEN=$TOKEN
ADMIN_IDS=$ADMIN_IDS
TIMEZONE=${timezone:-Europe/Moscow}
DB_PATH=data/autopost.db
LOG_LEVEL=${log_level:-INFO}
EOF
    chmod 600 "$tmp"
    mv "$tmp" "$ENV_FILE"
}

# ------------------------------------------------------------------------ окружение

find_python() {
    local candidate
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            PYTHON="$(command -v "$candidate")"
            return 0
        fi
    done
    return 1
}

setup_python() {
    find_python || die "Нужен Python 3.11 или новее (в Ubuntu 24.04 он есть: пакет python3)."
    if [ ! -x "$VENV/bin/python" ]; then
        info "Создаю отдельное окружение Python для бота ($VENV)…"
        if ! "$PYTHON" -m venv "$VENV" >/dev/null 2>&1; then
            rm -rf "$VENV"
            info "Ставлю системный пакет python3-venv — он нужен для отдельного окружения…"
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv >/dev/null ||
                { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv >/dev/null; }
            "$PYTHON" -m venv "$VENV"
        fi
    fi
    info "Устанавливаю библиотеки бота…"
    "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
    "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -r "$APP_DIR/requirements.txt"
    # Служба работает не от root и не может сама писать .pyc в папку с кодом — компилируем заранее
    "$VENV/bin/python" -m compileall -q "$APP_DIR/bot" >/dev/null
}

setup_user() {
    if ! id "$SERVICE_USER" >/dev/null 2>&1; then
        useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
    mkdir -p "$APP_DIR/data"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/data"
    chown "root:$SERVICE_USER" "$ENV_FILE"
    chmod 640 "$ENV_FILE"
    # Если папка лежит там, куда системному пользователю нельзя (например, в /root), работаем от root
    if runuser -u "$SERVICE_USER" -- test -r "$APP_DIR/bot/__main__.py" -a -x "$VENV/bin/python" 2>/dev/null; then
        RUN_AS="$SERVICE_USER"
    else
        RUN_AS="root"
        warn "Папка $APP_DIR недоступна пользователю $SERVICE_USER — бот будет работать от root."
        warn "Аккуратнее держать бота в /opt/autopost."
    fi
}

render_unit() {
    cat <<EOF
[Unit]
Description=Autopost - Telegram autoposting bot
Documentation=file://$APP_DIR/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_AS
WorkingDirectory=$APP_DIR
ExecStart=$VENV/bin/python -m bot
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
# Бережём соседние сервисы: пониженный приоритет и потолок памяти
Nice=5
MemoryMax=400M
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF
}

start_service() {
    render_unit >"$UNIT_FILE"
    systemctl daemon-reload
    systemctl enable "$SERVICE" >/dev/null 2>&1
    info "Запускаю службу $SERVICE…"
    local since
    since="$(date '+%Y-%m-%d %H:%M:%S')"
    systemctl restart "$SERVICE"
    local waited=0
    while [ "$waited" -lt 40 ]; do
        sleep 2
        waited=$((waited + 2))
        if journalctl -u "$SERVICE" --since "$since" --no-pager -q 2>/dev/null | grep -q "запущен"; then
            return 0
        fi
        if systemctl is-failed --quiet "$SERVICE"; then
            break
        fi
    done
    say ""
    journalctl -u "$SERVICE" --since "$since" --no-pager -q -n 30 2>/dev/null || true
    if systemctl is-active --quiet "$SERVICE"; then
        warn "Служба работает, но бот ещё не отчитался о запуске — посмотрите логи чуть позже."
        return 0
    fi
    die "Бот не запустился — причина в логах выше. Поменять настройки: sudo bash $APP_DIR/install.sh --reconfigure"
}

# ---------------------------------------------------------------------- действия

uninstall() {
    if [ -f "$UNIT_FILE" ]; then
        systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
        rm -f "$UNIT_FILE"
        systemctl daemon-reload
        info "Служба $SERVICE остановлена и удалена."
    else
        warn "Служба $SERVICE не установлена."
    fi
    say "Папка $APP_DIR (база data/ и настройки .env) осталась на месте. Удалить её: sudo rm -rf $APP_DIR"
}

update_code() {
    if [ -d "$APP_DIR/.git" ] && command -v git >/dev/null 2>&1; then
        info "Скачиваю обновление из GitHub…"
        git -C "$APP_DIR" pull --ff-only ||
            die "Не удалось скачать обновление из GitHub. Если сервер не видит GitHub, обновите из архива: sudo bash $APP_DIR/install.sh --update-zip /root/autopost.zip"
    else
        warn "Папка установлена не через git — код не скачиваю. Обновить из архива: sudo bash $APP_DIR/install.sh --update-zip /root/autopost.zip"
    fi
}

# Новая версия из ZIP-архива (например, скачанного с GitHub на компьютере и переданного через scp).
# Настройки (.env), база (data/) и окружение Python (.venv) остаются прежними.
update_from_zip() {
    local zip="$1" tmp main src
    [ -f "$zip" ] || die "Архив не найден: $zip"
    find_python || die "Нужен Python 3.11 или новее."
    tmp="$(mktemp -d)"
    info "Распаковываю $zip…"
    if ! "$PYTHON" - "$zip" "$tmp" <<'PY'; then
import sys
import zipfile

zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PY
        rm -rf "$tmp"
        die "Не удалось распаковать архив — скачайте его заново."
    fi
    # В архиве с GitHub всё лежит в папке вида autopost-ветка/
    main="$(find "$tmp" -maxdepth 3 -type f -path '*/bot/__main__.py' | head -n 1)"
    src="$(dirname "$(dirname "${main:-$tmp/x/y}")")"
    if [ -z "$main" ] || [ ! -f "$src/install.sh" ] || [ ! -f "$src/requirements.txt" ]; then
        rm -rf "$tmp"
        die "В архиве нет бота автопостинга (bot/, install.sh, requirements.txt)."
    fi
    rm -rf "$src/.env" "$src/data" "$src/.venv"
    info "Обновляю файлы бота (настройки .env и база data/ остаются как были)…"
    # Папку с кодом подменяем целиком, чтобы не остались файлы, удалённые в новой версии
    rm -rf "$APP_DIR/bot.new" "$APP_DIR/bot.old"
    cp -a "$src/bot" "$APP_DIR/bot.new"
    rm -rf "$src/bot"
    cp -a "$src/." "$APP_DIR/"
    mv "$APP_DIR/bot" "$APP_DIR/bot.old"
    mv "$APP_DIR/bot.new" "$APP_DIR/bot"
    rm -rf "$APP_DIR/bot.old" "$tmp"
}

check_foreign_unit() {
    [ -f "$UNIT_FILE" ] || return 0
    local other
    other="$(sed -n 's/^WorkingDirectory=//p' "$UNIT_FILE" | head -n 1)"
    if [ -n "$other" ] && [ "$other" != "$APP_DIR" ]; then
        die "Служба $SERVICE уже установлена из папки $other. Обновляйте бота там или сначала удалите ту установку: sudo bash $other/install.sh --uninstall"
    fi
}

print_summary() {
    local who="${BOT_USERNAME:+@$BOT_USERNAME}"
    say ""
    say "${GREEN}${BOLD}✅ Готово!${RESET} Бот ${who:-автопостинга} работает как служба $SERVICE."
    say ""
    say "Дальше: каждый админ открывает бота в Telegram и отправляет /start."
    say ""
    say "Полезные команды:"
    say "  журнал (логи):       journalctl -u $SERVICE -f"
    say "  состояние:           systemctl status $SERVICE"
    say "  перезапуск:          sudo systemctl restart $SERVICE"
    say "  токен/админы:        sudo bash $APP_DIR/install.sh --reconfigure"
    say "  обновить бота:       sudo bash $APP_DIR/install.sh --update"
    say "  обновить из архива:  sudo bash $APP_DIR/install.sh --update-zip /root/autopost.zip"
    say "  удалить службу:      sudo bash $APP_DIR/install.sh --uninstall"
}

main() {
    local args=("$@")
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --reconfigure) MODE="reconfigure" ;;
            --update) MODE="update" ;;
            --update-zip)
                [ "$#" -ge 2 ] || die "Укажите архив: --update-zip /root/autopost.zip"
                MODE="update-zip"
                ZIP_PATH="$2"
                shift
                ;;
            --update-zip=*)
                MODE="update-zip"
                ZIP_PATH="${1#*=}"
                ;;
            --after-update) MODE="updated" ;;
            --uninstall) MODE="uninstall" ;;
            --no-service) NO_SERVICE=1 ;;
            -h | --help)
                sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
                return 0
                ;;
            *) die "Неизвестный параметр: $1 (см. --help)" ;;
        esac
        shift
    done

    if [ ! -f "$APP_DIR/bot/__main__.py" ] || [ ! -f "$APP_DIR/requirements.txt" ]; then
        die "Запускайте install.sh из папки бота — рядом должны лежать bot/ и requirements.txt."
    fi

    if [ "$(id -u)" -ne 0 ] && [ "$NO_SERVICE" -eq 0 ]; then
        command -v sudo >/dev/null 2>&1 || die "Нужны права root: запустите от root."
        exec sudo --preserve-env=AUTOPOST_BOT_TOKEN,AUTOPOST_ADMIN_IDS bash "${BASH_SOURCE[0]}" "${args[@]}"
    fi
    if [ "$NO_SERVICE" -eq 0 ]; then
        [ -d /run/systemd/system ] || die "На сервере нет systemd — эта установка рассчитана на Ubuntu/Debian."
    fi

    say "${BOLD}Бот автопостинга${RESET} → $APP_DIR"
    case "$MODE" in
        uninstall)
            uninstall
            return 0
            ;;
        update | update-zip)
            if [ "$MODE" = "update" ]; then
                update_code
            else
                update_from_zip "$ZIP_PATH"
            fi
            # Дальше продолжает уже обновлённая версия скрипта
            exec bash "$APP_DIR/install.sh" --after-update
            ;;
        updated) MODE="update" ;;
    esac

    [ "$NO_SERVICE" -eq 1 ] || check_foreign_unit
    configure
    setup_python
    write_env
    : >"$MARKER"

    if [ "$NO_SERVICE" -eq 1 ]; then
        info "Окружение и настройки готовы (служба не создавалась). Запуск вручную: cd $APP_DIR && .venv/bin/python -m bot"
        return 0
    fi
    setup_user
    start_service
    print_summary
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    trap 'die "Установка прервана (строка $LINENO). Причина — в сообщениях выше."' ERR
    main "$@"
fi
