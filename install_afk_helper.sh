#!/usr/bin/env bash
# Установка AFK Helper: копирует программу, добавляет в меню и на рабочий стол.
# Запуск: bash install_afk_helper.sh   (из папки, где лежит afk_helper.py)
set -e

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -f "$SRC_DIR/afk_helper.py" ]; then
    echo "Не найден afk_helper.py рядом с установщиком ($SRC_DIR)."
    exit 1
fi

APP_DIR="$HOME/.local/share/afk-helper"
MENU_DIR="$HOME/.local/share/applications"
mkdir -p "$APP_DIR" "$MENU_DIR"

cp "$SRC_DIR/afk_helper.py" "$APP_DIR/afk_helper.py"
chmod +x "$APP_DIR/afk_helper.py"

# Папка рабочего стола (учитывает русское «Рабочий стол»)
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
[ -z "$DESKTOP_DIR" ] && DESKTOP_DIR="$HOME/Desktop"
mkdir -p "$DESKTOP_DIR"

ENTRY="[Desktop Entry]
Type=Application
Name=AFK Helper
Comment=Анти-AFK круг мышью и бинды команд в чат
Exec=/usr/bin/env python3 $APP_DIR/afk_helper.py
Icon=input-mouse
Terminal=false
Categories=Utility;Game;
StartupNotify=true
"

printf '%s' "$ENTRY" > "$MENU_DIR/afk-helper.desktop"
printf '%s' "$ENTRY" > "$DESKTOP_DIR/afk-helper.desktop"
chmod +x "$MENU_DIR/afk-helper.desktop" "$DESKTOP_DIR/afk-helper.desktop"

# GNOME/Ubuntu: пометить ярлык как доверенный, чтобы он запускался двойным кликом
gio set "$DESKTOP_DIR/afk-helper.desktop" metadata::trusted true 2>/dev/null || true
update-desktop-database "$MENU_DIR" 2>/dev/null || true

echo "Готово."
echo "  Программа:     $APP_DIR/afk_helper.py"
echo "  Ярлык:         $DESKTOP_DIR/afk-helper.desktop"
echo "  Также есть в меню приложений как «AFK Helper»."
echo "Обновить программу потом: положить новый afk_helper.py рядом и снова запустить этот скрипт."
