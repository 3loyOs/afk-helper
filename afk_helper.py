#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AFK Helper для Linux
  • круговое движение мыши раз в N секунд (анти-AFK)
  • глобальная горячая клавиша вкл/выкл (работает прямо в игре)
  • бинды: клавиша или комбинация -> текст, который печатается в чат игры

Ввод/вывод идёт через evdev/uinput (уровень ядра), поэтому работает
в X11, Wayland и в полноэкранных играх (включая Proton/Wine).
"""

import glob
import json
import math
import os
import queue
import random
import select
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

try:
    import evdev
    from evdev import UInput, InputDevice, ecodes as e
except ImportError:
    sys.stderr.write(
        "Не найден модуль evdev.\n"
        "  Debian/Ubuntu/Mint:  sudo apt install python3-evdev python3-tk\n"
        "  Arch:                sudo pacman -S python-evdev tk\n"
        "  Fedora:              sudo dnf install python3-evdev python3-tkinter\n"
        "  или:                 pip install --user evdev\n")
    sys.exit(1)

APP_NAME = "AFK Helper"
VDEV_PREFIX = "afk-helper-virtual"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config", "afk-helper", "config.json")

DEFAULT_CONFIG = {
    "toggle_key": "F8",
    "interval_sec": 120,
    "jitter_sec": 15,
    "radius": 40,
    "circle_duration": 0.8,
    "chat_open_key": "T",
    "chat_open_delay_ms": 150,
    "key_delay_ms": 12,
    "circle_enabled": True,
    "clickers": {"left": {"interval_ms": 100, "key": "F6"},
                 "right": {"interval_ms": 200, "key": "F7"}},
    "click_jitter_pct": 10,
    "walk_enabled": False,
    "walk_steps": [{"key": "W", "ms": 300}, {"key": "A", "ms": 300},
                   {"key": "S", "ms": 300}, {"key": "D", "ms": 300}],
    "walk_gap_ms": 100,
    "notify": True,
    "binds": [
        {"key": "KP5", "text": "/airdrop info", "enabled": True,
         "press_enter": True, "auto": False, "auto_interval": 60},
    ],
}

BIND_DEFAULTS = {"key": "", "text": "", "enabled": True,
                 "press_enter": True, "auto": False, "auto_interval": 60,
                 "mode": "chat", "keys": "", "key_gap_ms": 50}

CLICK_SIDES = (("left", "ЛКМ"), ("right", "ПКМ"))
CLICK_HELP = (
    "Рекомендуемые интервалы:\n"
    "• от 100 мс (до 10 кликов/с) — надёжно и похоже на человека, лучший выбор для онлайн-игр;\n"
    "• 50–100 мс (10–20 кликов/с) — быстро, большинство игр засчитывают каждый клик;\n"
    "• меньше 50 мс — игра может пропускать клики (кадр при 60 FPS длится ~17 мс),\n"
    "   а античит — заподозрить макрос;\n"
    "• минимум в программе — 20 мс. Кнопка зажимается примерно на 25 мс, потом отпускается.\n"
    "Пока окно программы активно, автокликер на паузе — чтобы его можно было выключить мышью.")


def parse_sequence(seq):
    """'1 2 Ctrl+E WAIT:500' -> шаги [('key', [моды], код) | ('wait', сек)], нераспознанное."""
    steps, bad = [], []
    for tok in seq.split():
        up = tok.upper()
        if up.startswith("WAIT:"):
            try:
                steps.append(("wait", max(0, int(up[5:])) / 1000.0))
            except ValueError:
                bad.append(tok)
            continue
        parts = tok.split("+")
        ok, mods = True, []
        for m in parts[:-1]:
            mm = m.capitalize()
            if mm in MOD_CODES:
                mods.append(MOD_CODES[mm])
            else:
                ok = False
        key = resolve_key(parts[-1].upper()) if parts[-1] else None
        if ok and key is not None:
            steps.append(("key", mods, key))
        else:
            bad.append(tok)
    return steps, bad


def bind_desc(b):
    if b.get("mode") == "keys":
        return "⌨ " + b.get("keys", "")
    return b.get("text", "")

PERMISSION_HELP = (
    "Нет доступа к устройствам ввода (/dev/input) или к /dev/uinput.\n\n"
    "Выполните один раз в терминале:\n\n"
    "sudo usermod -aG input $USER\n"
    "echo 'KERNEL==\"uinput\", GROUP=\"input\", MODE=\"0660\", "
    "OPTIONS+=\"static_node=uinput\"' | sudo tee /etc/udev/rules.d/99-uinput.rules\n"
    "echo uinput | sudo tee /etc/modules-load.d/uinput.conf\n"
    "sudo modprobe uinput\n"
    "sudo udevadm control --reload-rules && sudo udevadm trigger\n\n"
    "После этого выйдите из системы и зайдите снова."
)

# ---------------------------------------------------------------- клавиши --

MODIFIERS = {
    e.KEY_LEFTCTRL: "Ctrl", e.KEY_RIGHTCTRL: "Ctrl",
    e.KEY_LEFTSHIFT: "Shift", e.KEY_RIGHTSHIFT: "Shift",
    e.KEY_LEFTALT: "Alt", e.KEY_RIGHTALT: "Alt",
    e.KEY_LEFTMETA: "Super", e.KEY_RIGHTMETA: "Super",
}
MOD_ORDER = ["Ctrl", "Shift", "Alt", "Super"]
# Эти кнопки никогда не ловим как бинды, иначе мышью будет не пользоваться
IGNORED_CODES = {e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE, e.BTN_TOUCH}


def code_to_name(code):
    n = e.KEY.get(code) or e.BTN.get(code) or "CODE_%d" % code
    if isinstance(n, (list, tuple)):
        good = [x for x in n if "MIN_INTERESTING" not in x]
        n = (good or list(n))[0]
    return n[4:] if n.startswith("KEY_") else n


def name_to_code(name):
    if not name:
        return None
    for cand in ("KEY_" + name, name):
        c = getattr(e, cand, None)
        if isinstance(c, int):
            return c
    return None


def make_combo(mods, keyname):
    parts = [m for m in MOD_ORDER if m in mods]
    return "+".join(parts + [keyname])


def build_char_map():
    """Символ -> (код клавиши, нужен ли Shift). Раскладка: английская (US)."""
    m = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        code = getattr(e, "KEY_" + c.upper())
        m[c] = (code, False)
        m[c.upper()] = (code, True)
    for d, s in zip("1234567890", "!@#$%^&*()"):
        code = getattr(e, "KEY_" + d)
        m[d] = (code, False)
        m[s] = (code, True)
    pairs = [(" ", "KEY_SPACE", None), ("-", "KEY_MINUS", "_"), ("=", "KEY_EQUAL", "+"),
             ("[", "KEY_LEFTBRACE", "{"), ("]", "KEY_RIGHTBRACE", "}"),
             (";", "KEY_SEMICOLON", ":"), ("'", "KEY_APOSTROPHE", '"'),
             (",", "KEY_COMMA", "<"), (".", "KEY_DOT", ">"), ("/", "KEY_SLASH", "?"),
             ("\\", "KEY_BACKSLASH", "|"), ("`", "KEY_GRAVE", "~")]
    for ch, kn, sh in pairs:
        code = getattr(e, kn)
        m[ch] = (code, False)
        if sh:
            m[sh] = (code, True)
    m["\t"] = (e.KEY_TAB, False)
    return m


CHAR_MAP = build_char_map()
MOD_CODES = {"Ctrl": e.KEY_LEFTCTRL, "Shift": e.KEY_LEFTSHIFT,
             "Alt": e.KEY_LEFTALT, "Super": e.KEY_LEFTMETA}
PRESET_KEYS = ("binds", "chat_open_key", "chat_open_delay_ms", "key_delay_ms")


def resolve_key(name):
    if name.startswith("BTN") or name.startswith("CODE_"):
        return None
    return name_to_code(name)

# ----------------------------------------------------------------- конфиг --


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
    except FileNotFoundError:
        pass
    except Exception as ex:
        sys.stderr.write("Не удалось прочитать конфиг (%s), использую стандартный\n" % ex)
    presets = cfg.get("presets") if isinstance(cfg.get("presets"), dict) else {}
    active = cfg.get("active_preset") or "Основной"
    if active in presets:
        for k in PRESET_KEYS:
            if k in presets[active]:
                cfg[k] = presets[active][k]
    cfg["presets"] = presets
    cfg["active_preset"] = active
    # перенос старой общей настройки Enter в каждый бинд
    old_enter = bool(cfg.pop("press_enter", True))
    fixed = []
    for b in cfg.get("binds", []):
        nb = dict(BIND_DEFAULTS)
        nb["press_enter"] = old_enter
        nb.update(b)
        fixed.append(nb)
    cfg["binds"] = fixed
    clickers = json.loads(json.dumps(DEFAULT_CONFIG["clickers"]))
    for side in clickers:
        clickers[side].update((cfg.get("clickers") or {}).get(side, {}))
    cfg["clickers"] = clickers
    steps = cfg.get("walk_steps")
    if not (isinstance(steps, list) and len(steps) == 4
            and all(isinstance(s, dict) and "key" in s and "ms" in s for s in steps)):
        cfg["walk_steps"] = json.loads(json.dumps(DEFAULT_CONFIG["walk_steps"]))
    return cfg


def save_config(cfg):
    try:
        if cfg.get("active_preset"):
            cfg.setdefault("presets", {})[cfg["active_preset"]] = {
                k: json.loads(json.dumps(cfg[k])) for k in PRESET_KEYS if k in cfg}
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception as ex:
        sys.stderr.write("Не удалось сохранить конфиг: %s\n" % ex)

# ------------------------------------------------------- виртуальный ввод --


class VirtualInput:
    """Виртуальные клавиатура и мышь через /dev/uinput."""

    def __init__(self):
        self.kbd = UInput(name=VDEV_PREFIX + "-kbd")  # все клавиши
        self.mouse = UInput(
            {e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE],
             e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL]},
            name=VDEV_PREFIX + "-mouse")
        self.kbd_lock = threading.Lock()
        self.mouse_lock = threading.Lock()
        time.sleep(0.3)  # даём системе подхватить устройства

    def close(self):
        for d in (self.kbd, self.mouse):
            try:
                d.close()
            except Exception:
                pass

    def circle(self, radius, duration):
        """Плавный круг, который заканчивается в исходной точке."""
        steps = max(24, int(duration * 120))
        sx = sy = 0
        for i in range(1, steps + 1):
            a = 2 * math.pi * i / steps
            tx = radius * (math.cos(a) - 1)
            ty = radius * math.sin(a)
            dx, dy = round(tx - sx), round(ty - sy)
            if dx or dy:
                with self.mouse_lock:
                    if dx:
                        self.mouse.write(e.EV_REL, e.REL_X, dx)
                    if dy:
                        self.mouse.write(e.EV_REL, e.REL_Y, dy)
                    self.mouse.syn()
                sx += dx
                sy += dy
            time.sleep(duration / steps)

    def click(self, side, hold):
        code = e.BTN_LEFT if side == "left" else e.BTN_RIGHT
        with self.mouse_lock:
            self.mouse.write(e.EV_KEY, code, 1)
            self.mouse.syn()
        time.sleep(hold)
        with self.mouse_lock:
            self.mouse.write(e.EV_KEY, code, 0)
            self.mouse.syn()

    def _tap(self, code, shift, d):
        k = self.kbd
        if shift:
            k.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1)
            k.syn()
            time.sleep(d)
        k.write(e.EV_KEY, code, 1)
        k.syn()
        time.sleep(d)
        k.write(e.EV_KEY, code, 0)
        k.syn()
        if shift:
            time.sleep(d)
            k.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
            k.syn()
        time.sleep(d)

    def press_combo(self, mods, code, hold):
        k = self.kbd
        with self.kbd_lock:
            for m in mods:
                k.write(e.EV_KEY, m, 1)
                k.syn()
                time.sleep(0.01)
            k.write(e.EV_KEY, code, 1)
            k.syn()
            time.sleep(hold)
            k.write(e.EV_KEY, code, 0)
            k.syn()
            for m in reversed(mods):
                time.sleep(0.01)
                k.write(e.EV_KEY, m, 0)
                k.syn()

    def send_chat(self, text, chat_code, open_delay, key_delay, press_enter):
        """Открыть чат, напечатать текст, отправить. Возвращает пропущенные символы."""
        skipped = []
        with self.kbd_lock:
            if chat_code:
                self._tap(chat_code, False, key_delay)
                time.sleep(open_delay)
            for ch in text:
                item = CHAR_MAP.get(ch)
                if item is None:
                    skipped.append(ch)
                    continue
                self._tap(item[0], item[1], key_delay)
            if press_enter:
                time.sleep(max(key_delay, 0.03))
                self._tap(e.KEY_ENTER, False, key_delay)
        return skipped

# ---------------------------------------------- глобальный слушатель ввода --


class InputListener(threading.Thread):
    """Читает все клавиатуры и мыши напрямую из /dev/input."""

    def __init__(self, out_queue):
        super().__init__(daemon=True)
        self.q = out_queue
        self.devices = {}          # fd -> InputDevice
        self.held = set()
        self.held_lock = threading.Lock()
        self.stop_flag = False
        self.last_scan = 0
        self.perm_reported = False

    def mods_held(self):
        with self.held_lock:
            return any(c in MODIFIERS for c in self.held)

    def scan(self):
        self.last_scan = time.time()
        known = {d.path for d in self.devices.values()}
        paths = evdev.list_devices()
        if not paths and glob.glob("/dev/input/event*") and not self.perm_reported:
            self.perm_reported = True
            self.q.put(("perm", None))
        for path in paths:
            if path in known:
                continue
            try:
                dev = InputDevice(path)
            except OSError:
                continue
            try:
                ok = (not dev.name.startswith(VDEV_PREFIX)
                      and bool(dev.capabilities().get(e.EV_KEY)))
            except OSError:
                ok = False
            if ok:
                self.devices[dev.fd] = dev
            else:
                dev.close()

    def drop(self, fd):
        dev = self.devices.pop(fd, None)
        if dev:
            try:
                dev.close()
            except Exception:
                pass

    def run(self):
        while not self.stop_flag:
            if time.time() - self.last_scan > 3:
                self.scan()
            fds = list(self.devices)
            if not fds:
                time.sleep(0.5)
                continue
            try:
                ready, _, _ = select.select(fds, [], [], 0.5)
            except (OSError, ValueError):
                for fd in fds:
                    self.drop(fd)
                continue
            for fd in ready:
                dev = self.devices.get(fd)
                if dev is None:
                    continue
                try:
                    for ev in dev.read():
                        if ev.type == e.EV_KEY:
                            self.handle(ev.code, ev.value)
                except OSError:
                    self.drop(fd)  # устройство отключили

    def handle(self, code, value):
        with self.held_lock:
            if value == 1:
                self.held.add(code)
            elif value == 0:
                self.held.discard(code)
            mods = {MODIFIERS[c] for c in self.held if c in MODIFIERS}
        if value != 1 or code in MODIFIERS or code in IGNORED_CODES:
            return
        name = code_to_name(code)
        self.q.put(("key", make_combo(mods, name), name))

# --------------------------------------------------------------------- GUI --


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        save_config(self.cfg)  # при первом запуске создаёт пресет «Основной»
        self.q = queue.Queue()
        self.enabled = False
        self.next_move = 0.0
        self.capture_cb = None
        self.auto_next = {}            # индекс бинда -> время следующей автоотправки
        self.click_active = {s: False for s, _ in CLICK_SIDES}
        self.app_focused = False
        self.num_vars = {}
        self.record_cb = None
        self.typing_lock = threading.Lock()
        self.stop_event = threading.Event()

        self.vi = None
        try:
            self.vi = VirtualInput()
        except Exception as ex:
            self.root.after(200, lambda: messagebox.showerror(
                APP_NAME, "Не удалось создать виртуальное устройство: %s\n\n%s"
                % (ex, PERMISSION_HELP)))

        self.listener = InputListener(self.q)
        self.listener.start()

        self.build_ui()
        threading.Thread(target=self.afk_loop, daemon=True).start()
        for side, _ in CLICK_SIDES:
            threading.Thread(target=self.click_loop, args=(side,), daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(30, self.poll)
        self.root.after(250, self.tick)
        self.update_status()
        self.log("Запущено. Вкл/выкл AFK-режима: %s" % self.cfg["toggle_key"])

    # ------------------------------------------------------------- интерфейс
    def build_ui(self):
        r = self.root
        r.title(APP_NAME)
        r.minsize(560, 420)
        r.geometry("%dx%d" % (min(760, r.winfo_screenwidth() - 40),
                              min(760, r.winfo_screenheight() - 100)))
        try:
            ttk.Style().theme_use("clam")
        except tk.TclError:
            pass

        main = ttk.Frame(r, padding=10)
        main.pack(fill="both", expand=True)

        # --- статус
        top = ttk.Frame(main)
        top.pack(fill="x", pady=(0, 8))
        self.status_lbl = tk.Label(top, font=("TkDefaultFont", 13, "bold"),
                                   anchor="w", width=20)
        self.status_lbl.pack(side="left")
        ttk.Button(top, text="Вкл / Выкл", command=self.toggle).pack(side="left", padx=6)
        self.countdown_lbl = ttk.Label(top, text="")
        self.countdown_lbl.pack(side="left", padx=8)

        pr = ttk.Frame(main)
        pr.pack(fill="x", pady=(0, 6))
        ttk.Label(pr, text="Пресет:").pack(side="left")
        self.preset_var = tk.StringVar(value=self.cfg["active_preset"])
        self.preset_box = ttk.Combobox(pr, textvariable=self.preset_var, state="readonly",
                                       width=24, values=list(self.cfg["presets"]))
        self.preset_box.pack(side="left", padx=6)
        self.preset_box.bind("<<ComboboxSelected>>",
                             lambda ev: self.switch_preset(self.preset_var.get()))
        for text, cmd in (("Новый", self.new_preset), ("Копия", self.copy_preset),
                          ("Переименовать", self.rename_preset), ("Удалить", self.delete_preset)):
            ttk.Button(pr, text=text, command=cmd).pack(side="left", padx=2)
        ttk.Label(pr, text="бинды + настройки чата", foreground="gray").pack(side="left", padx=6)

        self.nb = ttk.Notebook(main)
        self.tab_afk = self.scroll_tab(self.nb, "AFK-режим")
        self.tab_click = self.scroll_tab(self.nb, "Автокликер")
        self.tab_binds = self.scroll_tab(self.nb, "Бинды")

        # --- анти-AFK
        afk = ttk.LabelFrame(self.tab_afk, text="Круг мышью + автоповтор биндов", padding=8)
        afk.pack(fill="x", pady=4)
        fields = [
            ("interval_sec", "Интервал между кругами, сек", 5, 3600, 5, int),
            ("jitter_sec", "Случайный разброс ±, сек", 0, 600, 1, int),
            ("radius", "Радиус круга, пикс", 2, 1000, 5, int),
            ("circle_duration", "Длительность круга, сек", 0.1, 10, 0.1, float),
        ]
        row = 0
        for key, label, lo, hi, step, typ in fields:
            self.num_field(afk, row, key, label, lo, hi, step, typ)
            row += 1

        ttk.Label(afk, text="Горячая клавиша вкл/выкл").grid(row=row, column=0, sticky="w", pady=2)
        self.toggle_key_lbl = ttk.Label(afk, text=self.cfg["toggle_key"], width=22,
                                        relief="sunken", anchor="center")
        self.toggle_key_lbl.grid(row=row, column=1, sticky="w", padx=6)
        ttk.Button(afk, text="Назначить", command=self.assign_toggle).grid(row=row, column=2, sticky="w")
        row += 1
        btns = ttk.Frame(afk)
        btns.grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.circle_var = tk.BooleanVar(value=bool(self.cfg.get("circle_enabled", True)))
        ttk.Checkbutton(btns, text="Круг мышью", variable=self.circle_var,
                        command=lambda: self.set_cfg("circle_enabled", self.circle_var.get())
                        ).pack(side="left", padx=(0, 12))
        ttk.Button(btns, text="Тест круга (через 3 сек)",
                   command=self.test_circle).pack(side="left")
        self.notify_var = tk.BooleanVar(value=bool(self.cfg.get("notify", True)))
        ttk.Checkbutton(btns, text="Уведомление при вкл/выкл", variable=self.notify_var,
                        command=lambda: self.set_cfg("notify", self.notify_var.get())
                        ).pack(side="left", padx=12)

        # --- чат
        wk = ttk.LabelFrame(self.tab_afk, text="Ходьба по кругу", padding=8)
        wk.pack(fill="x", pady=4)
        self.walk_var = tk.BooleanVar(value=bool(self.cfg.get("walk_enabled", False)))
        ttk.Checkbutton(wk, text="Ходить при каждом срабатывании AFK-режима", variable=self.walk_var,
                        command=lambda: self.set_cfg("walk_enabled", self.walk_var.get())
                        ).grid(row=0, column=0, columnspan=5, sticky="w", pady=(0, 4))
        self.walk_key_lbls = []
        for i, st in enumerate(self.cfg["walk_steps"]):
            row = i + 1
            ttk.Label(wk, text="Шаг %d" % (i + 1)).grid(row=row, column=0, sticky="w", pady=2)
            kl = ttk.Label(wk, text=st["key"], width=10, relief="sunken", anchor="center")
            kl.grid(row=row, column=1, padx=6)
            ttk.Button(wk, text="Назначить",
                       command=lambda i=i: self.assign_walk_key(i)).grid(row=row, column=2)
            var = tk.StringVar(value=str(st["ms"]))
            ttk.Spinbox(wk, from_=10, to=10000, increment=50, width=7,
                        textvariable=var).grid(row=row, column=3, padx=(10, 2))
            var.trace_add("write", lambda *a, i=i, v=var: self.on_walk_ms(i, v))
            ttk.Label(wk, text="мс удержания").grid(row=row, column=4, sticky="w")
            self.walk_key_lbls.append(kl)
        gf = ttk.Frame(wk)
        gf.grid(row=5, column=0, columnspan=5, sticky="w", pady=(6, 0))
        ttk.Label(gf, text="Пауза между шагами").pack(side="left")
        gvar = tk.StringVar(value=str(self.cfg.get("walk_gap_ms", 100)))
        ttk.Spinbox(gf, from_=0, to=5000, increment=50, width=7, textvariable=gvar).pack(side="left", padx=4)
        gvar.trace_add("write", lambda *a: self.on_num("walk_gap_ms", gvar, int, 0, 5000))
        ttk.Label(gf, text="мс").pack(side="left")
        ttk.Button(gf, text="Тест ходьбы (через 3 сек)", command=self.test_walk).pack(side="left", padx=12)
        ttk.Label(wk, foreground="gray", justify="left",
                  text="По умолчанию W A S D — это те же клавиши, что Ц Ф Ы В: программа жмёт\n"
                       "физические кнопки, раскладка не важна. Вперёд → влево → назад → вправо\n"
                       "с одинаковой длительностью возвращает персонажа примерно на место.\n"
                       "Круг мышью и ходьбу можно включать вместе или по отдельности."
                  ).grid(row=6, column=0, columnspan=5, sticky="w", pady=(6, 0))

        ck = ttk.LabelFrame(self.tab_click, text="Автокликер", padding=8)
        ck.pack(fill="x", pady=4)
        self.click_key_lbls, self.click_status_lbls = {}, {}
        for row, (side, title) in enumerate(CLICK_SIDES):
            ttk.Label(ck, text=title, width=5).grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=str(self.cfg["clickers"][side]["interval_ms"]))
            ttk.Spinbox(ck, from_=20, to=600000, increment=10, width=8,
                        textvariable=var).grid(row=row, column=1, sticky="w")
            var.trace_add("write", lambda *a, s=side, v=var: self.on_click_interval(s, v))
            ttk.Label(ck, text="мс").grid(row=row, column=2, sticky="w", padx=(2, 10))
            kl = ttk.Label(ck, text=self.cfg["clickers"][side]["key"] or "(нет)", width=14,
                           relief="sunken", anchor="center")
            kl.grid(row=row, column=3, padx=4)
            ttk.Button(ck, text="Назначить",
                       command=lambda s=side: self.assign_click_key(s)).grid(row=row, column=4, padx=2)
            st = tk.Label(ck, width=6, font=("TkDefaultFont", 10, "bold"))
            st.grid(row=row, column=5, padx=6)
            ttk.Button(ck, text="Вкл / Выкл",
                       command=lambda s=side: self.toggle_clicker(s)).grid(row=row, column=6)
            self.click_key_lbls[side], self.click_status_lbls[side] = kl, st
        jf = ttk.Frame(ck)
        jf.grid(row=2, column=0, columnspan=7, sticky="w", pady=(4, 0))
        ttk.Label(jf, text="Случайный разброс интервала ±").pack(side="left")
        jvar = tk.StringVar(value=str(self.cfg.get("click_jitter_pct", 10)))
        ttk.Spinbox(jf, from_=0, to=50, increment=1, width=5, textvariable=jvar).pack(side="left", padx=4)
        jvar.trace_add("write", lambda *a: self.on_num("click_jitter_pct", jvar, int, 0, 50))
        ttk.Label(jf, text="%").pack(side="left")
        ttk.Label(ck, text=CLICK_HELP, foreground="gray", justify="left").grid(
            row=3, column=0, columnspan=7, sticky="w", pady=(6, 0))
        self.update_click_status()

        chat = ttk.LabelFrame(self.tab_binds, text="Настройки чата (для биндов «Написать в чат»)", padding=8)
        chat.pack(fill="x", pady=4)
        ttk.Label(chat, text="Клавиша открытия чата").grid(row=0, column=0, sticky="w", pady=2)
        self.chat_key_lbl = ttk.Label(chat, width=22, relief="sunken", anchor="center",
                                      text=self.cfg["chat_open_key"] or "(нет)")
        self.chat_key_lbl.grid(row=0, column=1, sticky="w", padx=6)
        cb = ttk.Frame(chat)
        cb.grid(row=0, column=2, sticky="w")
        ttk.Button(cb, text="Назначить", command=self.assign_chat_key).pack(side="left")
        ttk.Button(cb, text="Без клавиши", command=self.clear_chat_key).pack(side="left", padx=4)
        self.num_field(chat, 1, "chat_open_delay_ms", "Пауза после открытия чата, мс", 0, 3000, 10, int)
        self.num_field(chat, 2, "key_delay_ms", "Пауза между нажатиями, мс", 1, 500, 1, int)

        # --- бинды
        bf = ttk.LabelFrame(self.tab_binds, text="Бинды и команды", padding=8)
        bf.pack(fill="both", expand=True, pady=4, before=chat)
        self.tree = ttk.Treeview(bf, columns=("key", "text", "enter", "auto", "on"),
                                 show="headings", height=10)
        for col, title, width, anchor in (("key", "Клавиша", 110, "center"),
                                          ("text", "Действие", 250, "w"),
                                          ("enter", "Enter", 55, "center"),
                                          ("auto", "Авто", 90, "center"),
                                          ("on", "Вкл", 45, "center")):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor=anchor)
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda ev: self.edit_bind())
        side = ttk.Frame(bf)
        side.pack(side="left", fill="y", padx=(8, 0))
        ttk.Button(side, text="Добавить", command=self.add_bind).pack(fill="x", pady=2)
        ttk.Button(side, text="Изменить", command=self.edit_bind).pack(fill="x", pady=2)
        ttk.Button(side, text="Удалить", command=self.delete_bind).pack(fill="x", pady=2)
        ttk.Button(side, text="Вкл/выкл бинд", command=self.toggle_bind).pack(fill="x", pady=2)
        self.refresh_binds()

        # --- лог
        lf = ttk.LabelFrame(main, text="Журнал", padding=4)
        lf.pack(side="bottom", fill="x", pady=(4, 0))
        self.nb.pack(fill="both", expand=True, pady=(0, 4))
        self.log_txt = tk.Text(lf, height=4, state="disabled", wrap="word")
        self.log_txt.pack(fill="both", expand=True)

        ttk.Label(self.tab_binds, foreground="gray", justify="left",
                  text="Текст печатается в английской раскладке — держите её включённой в игре.\n"
                       "Автоотправка работает, пока включён AFK-режим (горячая клавиша вкл/выкл).\n"
                       "Если чат открывается клавишей «/», не начинайте текст со «/».").pack(anchor="w")

    def scroll_tab(self, nb, title):
        """Вкладка с вертикальной прокруткой (колесо мыши и полоса справа)."""
        outer = ttk.Frame(nb)
        nb.add(outer, text=title)
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0, yscrollincrement=20)
        try:
            bg = ttk.Style().lookup("TFrame", "background")
            if bg:
                canvas.configure(background=bg)
        except tk.TclError:
            pass
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas, padding=8)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda ev: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda ev: canvas.itemconfigure(win, width=ev.width))

        def wheel(ev):
            if canvas.yview() == (0.0, 1.0):
                return
            num = getattr(ev, "num", None)
            if num == 4:
                step = -1
            elif num == 5:
                step = 1
            else:
                step = -1 if ev.delta > 0 else 1
            canvas.yview_scroll(step * 3, "units")

        def bind_wheel(_ev):
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                canvas.bind_all(seq, wheel)

        def unbind_wheel(_ev):
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                canvas.unbind_all(seq)
        outer.bind("<Enter>", bind_wheel)
        outer.bind("<Leave>", unbind_wheel)
        return inner

    def num_field(self, parent, row, key, label, lo, hi, step, typ):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        var = tk.StringVar(value=str(self.cfg[key]))
        ttk.Spinbox(parent, from_=lo, to=hi, increment=step, textvariable=var,
                    width=10).grid(row=row, column=1, sticky="w", padx=6)
        var.trace_add("write", lambda *a: self.on_num(key, var, typ, lo, hi))
        self.num_vars[key] = var
        setattr(self, "var_" + key, var)

    def on_num(self, key, var, typ, lo, hi):
        try:
            val = typ(float(var.get().replace(",", ".")))
        except ValueError:
            return
        val = min(max(val, lo), hi)
        self.set_cfg(key, val)
        if key in ("interval_sec", "jitter_sec") and self.enabled:
            self.schedule_next()

    def set_cfg(self, key, val):
        self.cfg[key] = val
        save_config(self.cfg)

    def log(self, text):
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        lines = int(self.log_txt.index("end-1c").split(".")[0])
        if lines > 300:
            self.log_txt.delete("1.0", "%d.0" % (lines - 300))
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    # ------------------------------------------------------ назначение клавиш
    def start_capture(self, label, callback, focus_widget=None):
        (focus_widget or self.root).focus_set()  # чтобы нажатие не ушло в поле ввода
        label.config(text="Нажмите… (Esc — отмена)")
        self.capture_cb = callback

    def assign_toggle(self):
        def done(combo, name):
            if combo and name != "ESC":
                owner = self.key_owner(combo, ("toggle",))
                if owner:
                    messagebox.showwarning(APP_NAME, "%s уже используется: %s." % (combo, owner))
                else:
                    self.set_cfg("toggle_key", combo)
                    self.log("Горячая клавиша вкл/выкл: %s" % combo)
            self.toggle_key_lbl.config(text=self.cfg["toggle_key"])
        self.start_capture(self.toggle_key_lbl, done)

    def assign_chat_key(self):
        def done(combo, name):
            if name and name != "ESC":
                self.set_cfg("chat_open_key", name)
            self.chat_key_lbl.config(text=self.cfg["chat_open_key"] or "(нет)")
        self.start_capture(self.chat_key_lbl, done)

    def clear_chat_key(self):
        self.set_cfg("chat_open_key", "")
        self.chat_key_lbl.config(text="(нет)")

    # --------------------------------------------------------------- пресеты
    def ask_preset_name(self, title, initial=""):
        name = simpledialog.askstring(APP_NAME, title, initialvalue=initial, parent=self.root)
        if name is None or not name.strip():
            return None
        name = name.strip()
        if name in self.cfg["presets"]:
            messagebox.showwarning(APP_NAME, "Пресет «%s» уже есть." % name)
            return None
        return name

    def load_preset(self, name):
        p = self.cfg["presets"][name]
        for k in PRESET_KEYS:
            src = p[k] if k in p else DEFAULT_CONFIG[k]
            self.cfg[k] = json.loads(json.dumps(src))
        self.cfg["binds"] = [dict(BIND_DEFAULTS, **b) for b in self.cfg["binds"]]
        self.cfg["active_preset"] = name
        save_config(self.cfg)
        self.refresh_preset_ui()
        self.log("Пресет: %s" % name)

    def refresh_preset_ui(self):
        self.preset_box["values"] = list(self.cfg["presets"])
        self.preset_var.set(self.cfg["active_preset"])
        self.chat_key_lbl.config(text=self.cfg["chat_open_key"] or "(нет)")
        for k in ("chat_open_delay_ms", "key_delay_ms"):
            if k in self.num_vars:
                self.num_vars[k].set(str(self.cfg[k]))
        if hasattr(self, "mode_var"):
            self.mode_var.set(self.cfg.get("type_mode", "scancode"))
        self.refresh_binds()

    def switch_preset(self, name):
        if name and name != self.cfg["active_preset"] and name in self.cfg["presets"]:
            save_config(self.cfg)
            self.load_preset(name)

    def new_preset(self):
        name = self.ask_preset_name("Название нового пресета (например, название игры):")
        if name:
            save_config(self.cfg)
            p = {k: json.loads(json.dumps(DEFAULT_CONFIG[k])) for k in PRESET_KEYS}
            p["binds"] = []
            self.cfg["presets"][name] = p
            self.load_preset(name)

    def copy_preset(self):
        name = self.ask_preset_name("Название копии:", self.cfg["active_preset"] + " (копия)")
        if name:
            save_config(self.cfg)
            self.cfg["presets"][name] = json.loads(json.dumps(
                self.cfg["presets"][self.cfg["active_preset"]]))
            self.load_preset(name)

    def rename_preset(self):
        old = self.cfg["active_preset"]
        name = self.ask_preset_name("Новое название пресета:", old)
        if name:
            save_config(self.cfg)
            self.cfg["presets"][name] = self.cfg["presets"].pop(old)
            self.cfg["active_preset"] = name
            save_config(self.cfg)
            self.refresh_preset_ui()

    def delete_preset(self):
        if len(self.cfg["presets"]) <= 1:
            messagebox.showinfo(APP_NAME, "Это единственный пресет — его нельзя удалить.")
            return
        old = self.cfg["active_preset"]
        if not messagebox.askyesno(APP_NAME, "Удалить пресет «%s» со всеми его биндами?" % old):
            return
        del self.cfg["presets"][old]
        self.load_preset(next(iter(self.cfg["presets"])))

    def key_owner(self, combo, exclude=None):
        """Кому уже назначена клавиша (None — свободна)."""
        if not combo:
            return None
        if exclude != ("toggle",) and combo == self.cfg["toggle_key"]:
            return "вкл/выкл AFK-режима"
        for side, title in CLICK_SIDES:
            if exclude != ("click", side) and combo == self.cfg["clickers"][side]["key"]:
                return "автокликер " + title
        for j, b in enumerate(self.cfg["binds"]):
            if exclude != ("bind", j) and b["key"] == combo:
                return "бинд «%s»" % bind_desc(b)
        return None

    # ------------------------------------------------------------ автокликер
    def on_click_interval(self, side, var):
        try:
            v = int(float(var.get().replace(",", ".")))
        except ValueError:
            return
        self.cfg["clickers"][side]["interval_ms"] = min(max(v, 20), 600000)
        save_config(self.cfg)

    def assign_click_key(self, side):
        lbl = self.click_key_lbls[side]

        def done(combo, name):
            if name != "ESC":
                owner = self.key_owner(combo, ("click", side))
                if owner:
                    messagebox.showwarning(APP_NAME, "%s уже используется: %s." % (combo, owner))
                else:
                    self.cfg["clickers"][side]["key"] = combo
                    save_config(self.cfg)
            lbl.config(text=self.cfg["clickers"][side]["key"] or "(нет)")
        self.start_capture(lbl, done)

    def toggle_clicker(self, side):
        self.click_active[side] = not self.click_active[side]
        on = self.click_active[side]
        title = dict(CLICK_SIDES)[side]
        self.update_click_status()
        self.log("Автокликер %s %s (%d мс)" % (title, "включён" if on else "выключен",
                                              self.cfg["clickers"][side]["interval_ms"]))
        self.signal(on, "Автокликер %s %s" % (title, "включён" if on else "выключен"))

    def update_click_status(self):
        for side, _ in CLICK_SIDES:
            on = self.click_active[side]
            self.click_status_lbls[side].config(text="ВКЛ" if on else "ВЫКЛ",
                                                fg="#1a9c3a" if on else "#c0392b")

    def click_loop(self, side):
        while not self.stop_event.is_set():
            if not self.click_active[side] or self.app_focused or not self.vi:
                time.sleep(0.05)
                continue
            interval = self.cfg["clickers"][side]["interval_ms"] / 1000.0
            j = self.cfg.get("click_jitter_pct", 0) / 100.0
            interval *= random.uniform(1 - j, 1 + j)
            hold = min(0.025, interval * 0.4)
            t0 = time.time()
            try:
                self.vi.click(side, hold)
            except Exception as ex:
                self.q.put(("log", "Ошибка клика: %s" % ex))
                time.sleep(1)
            rest = interval - (time.time() - t0)
            if rest > 0:
                self.stop_event.wait(rest)

    # ----------------------------------------------------------------- бинды
    def refresh_binds(self):
        self.tree.delete(*self.tree.get_children())
        for i, b in enumerate(self.cfg["binds"]):
            auto = "каждые %d с" % b["auto_interval"] if b.get("auto") else "—"
            self.tree.insert("", "end", iid=str(i), values=(
                b["key"] or "(нет)", bind_desc(b),
                ("✓" if b.get("press_enter", True) else "—") if b.get("mode") != "keys" else "",
                auto,
                "✓" if b.get("enabled", True) else "—"))
        self.reschedule_auto()

    def reschedule_auto(self):
        now = time.time()
        self.auto_next = {i: now + b["auto_interval"]
                          for i, b in enumerate(self.cfg["binds"])
                          if b.get("auto") and b.get("enabled", True)}

    def selected_index(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def add_bind(self):
        self.bind_dialog(None)

    def edit_bind(self):
        i = self.selected_index()
        if i is not None:
            self.bind_dialog(i)

    def delete_bind(self):
        i = self.selected_index()
        if i is not None and messagebox.askyesno(APP_NAME, "Удалить бинд %s?" % self.cfg["binds"][i]["key"]):
            del self.cfg["binds"][i]
            save_config(self.cfg)
            self.refresh_binds()

    def toggle_bind(self):
        i = self.selected_index()
        if i is not None:
            b = self.cfg["binds"][i]
            b["enabled"] = not b.get("enabled", True)
            save_config(self.cfg)
            self.refresh_binds()
            self.tree.selection_set(str(i))

    def bind_dialog(self, index):
        b = dict(BIND_DEFAULTS)
        if index is not None:
            b.update(self.cfg["binds"][index])
        dlg = tk.Toplevel(self.root)
        dlg.title("Бинд")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        f = ttk.Frame(dlg, padding=10)
        f.pack(fill="both")

        # --- клавиша запуска
        ttk.Label(f, text="Клавиша запуска").grid(row=0, column=0, sticky="w")
        key_lbl = ttk.Label(f, text=b["key"] or "(не задана)", width=24, relief="sunken", anchor="center")
        key_lbl.grid(row=0, column=1, padx=6, pady=4, sticky="w")
        state = {"key": b["key"]}

        def captured(combo, name):
            if name != "ESC":
                state["key"] = combo
            key_lbl.config(text=state["key"] or "(не задана)")
        kb = ttk.Frame(f)
        kb.grid(row=0, column=2, sticky="w")
        ttk.Button(kb, text="Назначить",
                   command=lambda: (stop_rec(), self.start_capture(key_lbl, captured, dlg))).pack(side="left")
        ttk.Button(kb, text="Убрать",
                   command=lambda: (state.update(key=""), key_lbl.config(text="(не задана)"))
                   ).pack(side="left", padx=4)

        # --- режим
        mode_var = tk.StringVar(value=b.get("mode", "chat"))
        mf = ttk.Frame(f)
        mf.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 2))
        ttk.Label(mf, text="Что делать:").pack(side="left")
        ttk.Radiobutton(mf, text="Написать в чат", value="chat", variable=mode_var,
                        command=lambda: show_mode()).pack(side="left", padx=6)
        ttk.Radiobutton(mf, text="Нажать клавиши (без чата)", value="keys", variable=mode_var,
                        command=lambda: show_mode()).pack(side="left")

        # --- режим «чат»
        chat_f = ttk.Frame(f)
        chat_f.grid(row=2, column=0, columnspan=3, sticky="we")
        ttk.Label(chat_f, text="Текст", width=10).grid(row=0, column=0, sticky="w")
        text_var = tk.StringVar(value=b["text"])
        ent = ttk.Entry(chat_f, textvariable=text_var, width=46)
        ent.grid(row=0, column=1, padx=6, pady=4, sticky="we")
        enter_var = tk.BooleanVar(value=b.get("press_enter", True))
        ttk.Checkbutton(chat_f, text="Нажимать Enter (сразу отправить)",
                        variable=enter_var).grid(row=1, column=1, sticky="w", padx=6)

        # --- режим «клавиши»
        keys_f = ttk.Frame(f)
        keys_f.grid(row=3, column=0, columnspan=3, sticky="we")
        ttk.Label(keys_f, text="Клавиши", width=10).grid(row=0, column=0, sticky="w")
        keys_var = tk.StringVar(value=b.get("keys", ""))
        ttk.Entry(keys_f, textvariable=keys_var, width=46).grid(row=0, column=1, padx=6, pady=4, sticky="we")
        kbtn = ttk.Frame(keys_f)
        kbtn.grid(row=1, column=1, sticky="w", padx=6)
        rec_btn = ttk.Button(kbtn, text="● Записать")
        rec_btn.pack(side="left")
        ttk.Button(kbtn, text="Очистить", command=lambda: keys_var.set("")).pack(side="left", padx=4)
        gf = ttk.Frame(keys_f)
        gf.grid(row=2, column=1, sticky="w", padx=6, pady=(4, 0))
        ttk.Label(gf, text="Пауза между нажатиями").pack(side="left")
        gap_var = tk.StringVar(value=str(b.get("key_gap_ms", 50)))
        ttk.Spinbox(gf, from_=0, to=10000, increment=10, width=7, textvariable=gap_var).pack(side="left", padx=4)
        ttk.Label(gf, text="мс").pack(side="left")
        ttk.Label(keys_f, foreground="gray", justify="left",
                  text="Нажмите «Записать» и по очереди жмите нужные клавиши, Esc — стоп.\n"
                       "Можно писать вручную через пробел: 1 2 Q Ctrl+E SPACE KP5 F1\n"
                       "WAIT:500 — дополнительная пауза 500 мс в этом месте."
                  ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        def stop_rec():
            if self.record_cb is on_rec:
                self.record_cb = None
            rec_btn.config(text="● Записать", command=start_rec)

        def on_rec(combo):
            if combo is None:
                stop_rec()
                return
            keys_var.set((keys_var.get().strip() + " " + combo).strip())

        def start_rec():
            self.capture_cb = None
            self.record_cb = on_rec
            rec_btn.config(text="■ Стоп", command=stop_rec)
            dlg.focus_set()  # чтобы нажатия не попадали в поля ввода
        rec_btn.config(command=start_rec)

        def show_mode():
            if mode_var.get() == "keys":
                chat_f.grid_remove()
                keys_f.grid()
            else:
                stop_rec()
                keys_f.grid_remove()
                chat_f.grid()

        # --- общие настройки
        en_var = tk.BooleanVar(value=b.get("enabled", True))
        ttk.Checkbutton(f, text="Включён", variable=en_var).grid(row=4, column=1, sticky="w", padx=6, pady=(6, 0))
        auto_box = ttk.Frame(f)
        auto_box.grid(row=5, column=1, columnspan=2, sticky="w", padx=6, pady=(4, 0))
        auto_var = tk.BooleanVar(value=b.get("auto", False))
        ttk.Checkbutton(auto_box, text="Автоповтор каждые", variable=auto_var).pack(side="left")
        interval_var = tk.StringVar(value=str(b.get("auto_interval", 60)))
        ttk.Spinbox(auto_box, from_=5, to=86400, increment=5, width=7,
                    textvariable=interval_var).pack(side="left", padx=4)
        ttk.Label(auto_box, text="сек (пока включён AFK-режим)").pack(side="left")
        ttk.Label(f, foreground="gray",
                  text="Клавиша запуска не обязательна, если бинд нужен только для автоповтора."
                  ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))
        show_mode()

        def num(var, default):
            try:
                return int(float(var.get().replace(",", ".")))
            except ValueError:
                return default

        def save():
            stop_rec()
            key, mode = state["key"], mode_var.get()
            text, keys = text_var.get(), " ".join(keys_var.get().split())
            auto, interval, gap = auto_var.get(), num(interval_var, 0), max(0, num(gap_var, 50))
            if auto and interval < 5:
                messagebox.showwarning(APP_NAME, "Интервал автоповтора — от 5 секунд.", parent=dlg)
                return
            if not key and not auto:
                messagebox.showwarning(APP_NAME, "Назначьте клавишу запуска или включите автоповтор.", parent=dlg)
                return
            if mode == "chat":
                if not text.strip():
                    messagebox.showwarning(APP_NAME, "Введите текст.", parent=dlg)
                    return
                if auto and not enter_var.get() and not messagebox.askyesno(
                        APP_NAME, "Автоповтор без Enter будет только дописывать текст в чат "
                                  "и не отправлять его. Так и задумано?", parent=dlg):
                    return
                bad = self.unprintable(text)
                if bad and not messagebox.askyesno(
                        APP_NAME, "Эти символы не напечатаются: %s\nСохранить всё равно?"
                        % " ".join(bad), parent=dlg):
                    return
            else:
                steps, bad = parse_sequence(keys)
                if bad:
                    messagebox.showwarning(APP_NAME, "Не получится нажать: %s\n"
                                           "(кнопки мыши в последовательности не поддерживаются)"
                                           % " ".join(bad), parent=dlg)
                    return
                if not any(s[0] == "key" for s in steps):
                    messagebox.showwarning(APP_NAME, "Добавьте хотя бы одну клавишу.", parent=dlg)
                    return
            owner = self.key_owner(key, ("bind", index))
            if owner:
                messagebox.showwarning(APP_NAME, "%s уже используется: %s." % (key, owner), parent=dlg)
                return
            new = {"key": key, "mode": mode, "text": text, "keys": keys, "key_gap_ms": gap,
                   "enabled": en_var.get(), "press_enter": enter_var.get(), "auto": auto,
                   "auto_interval": interval if interval >= 5 else b.get("auto_interval", 60)}
            if index is None:
                self.cfg["binds"].append(new)
            else:
                self.cfg["binds"][index] = new
            save_config(self.cfg)
            self.refresh_binds()
            self.capture_cb = None
            dlg.destroy()

        def cancel():
            stop_rec()
            self.capture_cb = None
            dlg.destroy()

        bb = ttk.Frame(f)
        bb.grid(row=7, column=0, columnspan=3, pady=(8, 0), sticky="e")
        ttk.Button(bb, text="Сохранить", command=save).pack(side="left", padx=4)
        ttk.Button(bb, text="Отмена", command=cancel).pack(side="left")
        dlg.protocol("WM_DELETE_WINDOW", cancel)
        if mode_var.get() == "chat":
            ent.focus_set()

    # ----------------------------------------------------------------- логика
    def app_has_focus(self):
        try:
            return self.root.focus_get() is not None
        except (KeyError, tk.TclError):
            return True

    def poll(self):
        self.app_focused = self.app_has_focus()
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "key":
                    self.on_key(msg[1], msg[2])
                elif msg[0] == "log":
                    self.log(msg[1])
                elif msg[0] == "perm":
                    self.log("Нет доступа к /dev/input — см. инструкцию.")
                    messagebox.showerror(APP_NAME, PERMISSION_HELP)
        except queue.Empty:
            pass
        self.root.after(30, self.poll)

    def on_key(self, combo, name):
        if self.record_cb:
            self.record_cb(None if name == "ESC" else combo)
            return
        if self.capture_cb:
            cb, self.capture_cb = self.capture_cb, None
            cb(combo, name)
            return
        if combo == self.cfg["toggle_key"]:
            self.toggle()
            return
        for side, _ in CLICK_SIDES:
            if combo == self.cfg["clickers"][side]["key"]:
                self.toggle_clicker(side)
                return
        if self.app_has_focus():
            return  # не печатаем бинды в собственные поля программы
        for b in self.cfg["binds"]:
            if b.get("enabled", True) and b["key"] and b["key"] == combo:
                self.fire_bind(b)
                return

    def fire_bind(self, b, auto=False):
        """Запускает печать. Возвращает False, если сейчас печатать нельзя."""
        if not self.vi:
            self.log("Виртуальная клавиатура недоступна.")
            return False
        if not self.typing_lock.acquire(blocking=False):
            if not auto:
                self.log("Предыдущая команда ещё печатается, пропуск.")
            return False
        if b.get("mode") == "keys":
            self.run_key_sequence(b, auto)
            return True
        text = b["text"]
        chat_code = name_to_code(self.cfg["chat_open_key"])
        open_delay = self.cfg["chat_open_delay_ms"] / 1000.0
        key_delay = self.cfg["key_delay_ms"] / 1000.0
        enter = bool(b.get("press_enter", True))
        label = "Авто" if auto else "Бинд %s" % b["key"]

        def worker():
            try:
                t0 = time.time()  # ждём, пока отпустят Ctrl/Shift/Alt из комбинации
                while self.listener.mods_held() and time.time() - t0 < 2:
                    time.sleep(0.02)
                time.sleep(0.05)
                skipped = self.vi.send_chat(text, chat_code, open_delay, key_delay, enter)
                msg = "%s → «%s»%s" % (label, text, "" if enter else " (без Enter)")
                if skipped:
                    msg += " (пропущено: %s)" % "".join(sorted(set(skipped)))
                self.q.put(("log", msg))
            except Exception as ex:
                self.q.put(("log", "Ошибка печати: %s" % ex))
            finally:
                self.typing_lock.release()
        threading.Thread(target=worker, daemon=True).start()
        return True

    def run_key_sequence(self, b, auto):
        """Нажимает клавиши по очереди, без чата. typing_lock уже захвачен."""
        steps, _ = parse_sequence(b.get("keys", ""))
        gap = max(0, b.get("key_gap_ms", 50)) / 1000.0
        label = "Авто" if auto else "Бинд %s" % b["key"]

        def worker():
            try:
                t0 = time.time()
                while self.listener.mods_held() and time.time() - t0 < 2:
                    time.sleep(0.02)
                time.sleep(0.05)
                for i, st in enumerate(steps):
                    if st[0] == "wait":
                        time.sleep(st[1])
                        continue
                    self.vi.press_combo(st[1], st[2], 0.03)
                    if i < len(steps) - 1:
                        time.sleep(gap)
                self.q.put(("log", "%s → клавиши: %s" % (label, b.get("keys", ""))))
            except Exception as ex:
                self.q.put(("log", "Ошибка нажатия клавиш: %s" % ex))
            finally:
                self.typing_lock.release()
        threading.Thread(target=worker, daemon=True).start()

    def run_auto_sends(self):
        now = time.time()
        for i, due in list(self.auto_next.items()):
            if now < due or i >= len(self.cfg["binds"]):
                continue
            b = self.cfg["binds"][i]
            if self.app_has_focus():
                self.auto_next[i] = now + 2   # не печатаем в окно самой программы
                continue
            if self.fire_bind(b, auto=True):
                self.auto_next[i] = now + b["auto_interval"]
            else:
                self.auto_next[i] = now + 1   # занято другой командой — повтор через секунду

    def schedule_next(self):
        j = self.cfg["jitter_sec"]
        delay = self.cfg["interval_sec"] + random.uniform(-j, j)
        self.next_move = time.time() + max(5, delay)

    def toggle(self):
        self.enabled = not self.enabled
        if self.enabled:
            self.schedule_next()
            self.reschedule_auto()
        self.update_status()
        state = "включён" if self.enabled else "выключен"
        self.log("AFK-режим %s" % state)
        self.signal(self.enabled, "AFK-режим " + state)

    def unprintable(self, text):
        return sorted({ch for ch in text if ch not in CHAR_MAP})

    def signal(self, on, text):
        if self.cfg.get("notify", True):
            try:
                subprocess.Popen(["notify-send", "-t", "1500", APP_NAME, text],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass

    def update_status(self):
        if self.enabled:
            self.status_lbl.config(text="● AFK-режим: ВКЛ", fg="#1a9c3a")
        else:
            self.status_lbl.config(text="● AFK-режим: ВЫКЛ", fg="#c0392b")

    def tick(self):
        if self.enabled:
            parts = []
            if self.cfg.get("circle_enabled", True) or self.cfg.get("walk_enabled", False):
                left = max(0, int(self.next_move - time.time()))
                parts.append("движение через %d:%02d" % (left // 60, left % 60))
            if self.auto_next:
                parts.append("автокоманд: %d" % len(self.auto_next))
            self.countdown_lbl.config(text=" · ".join(parts))
            self.run_auto_sends()
        else:
            self.countdown_lbl.config(text="")
        self.root.after(250, self.tick)

    def afk_loop(self):
        while not self.stop_event.is_set():
            circle = self.cfg.get("circle_enabled", True)
            walk = self.cfg.get("walk_enabled", False)
            if (self.enabled and self.vi and (circle or walk)
                    and time.time() >= self.next_move):
                if circle:
                    try:
                        self.vi.circle(self.cfg["radius"], self.cfg["circle_duration"])
                        self.q.put(("log", "Круг мышью"))
                    except Exception as ex:
                        self.q.put(("log", "Ошибка движения мыши: %s" % ex))
                if walk and self.enabled:
                    if self.app_focused:
                        self.q.put(("log", "Ходьба пропущена: активно окно программы"))
                    else:
                        self.do_walk()
                self.schedule_next()
            time.sleep(0.2)

    def do_walk(self):
        """Шаги по очереди. Держит typing_lock, чтобы не смешаться с печатью в чат."""
        got = self.typing_lock.acquire(timeout=10)
        try:
            done = []
            for st in self.cfg.get("walk_steps", []):
                if self.stop_event.is_set():
                    break
                code = resolve_key(st.get("key", ""))
                if code is None:
                    continue
                self.vi.press_combo([], code, max(10, int(st.get("ms", 300))) / 1000.0)
                done.append(st["key"])
                time.sleep(self.cfg.get("walk_gap_ms", 100) / 1000.0)
            self.q.put(("log", "Ходьба: " + " → ".join(done)))
        except Exception as ex:
            self.q.put(("log", "Ошибка ходьбы: %s" % ex))
        finally:
            if got:
                self.typing_lock.release()

    def test_walk(self):
        if not self.vi:
            return
        self.log("Тестовая ходьба через 3 секунды — переключитесь в игру…")

        def run():
            time.sleep(3)
            self.do_walk()
        threading.Thread(target=run, daemon=True).start()

    def assign_walk_key(self, i):
        lbl = self.walk_key_lbls[i]

        def done(combo, name):
            if name != "ESC":
                if resolve_key(name) is None:
                    messagebox.showwarning(APP_NAME, "Эту клавишу нельзя использовать для ходьбы.")
                else:
                    self.cfg["walk_steps"][i]["key"] = name
                    save_config(self.cfg)
            lbl.config(text=self.cfg["walk_steps"][i]["key"])
        self.start_capture(lbl, done)

    def on_walk_ms(self, i, var):
        try:
            v = int(float(var.get().replace(",", ".")))
        except ValueError:
            return
        self.cfg["walk_steps"][i]["ms"] = min(max(v, 10), 10000)
        save_config(self.cfg)

    def test_circle(self):
        if not self.vi:
            return
        self.log("Тестовый круг через 3 секунды…")

        def run():
            time.sleep(3)
            self.vi.circle(self.cfg["radius"], self.cfg["circle_duration"])
        threading.Thread(target=run, daemon=True).start()

    def on_close(self):
        self.click_active = {s: False for s, _ in CLICK_SIDES}
        self.stop_event.set()
        self.listener.stop_flag = True
        save_config(self.cfg)
        if self.vi:
            self.vi.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
