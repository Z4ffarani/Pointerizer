"""Pointerizer — record mouse/keyboard actions, replay them, schedule via Task Scheduler.

Hotkeys while recording:  F8 = checkpoint (review/redo)   F9 = stop & save
Playback abort: press Esc, or slam the mouse into any screen corner (pyautogui failsafe).
"""
import argparse
import ctypes
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Match pyautogui's DPI awareness so recorded and replayed coordinates agree on scaled displays.
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

from pynput import mouse, keyboard
from pynput.keyboard import Key

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
RECORDINGS_DIR = BASE_DIR / "recordings"

CHECKPOINT_KEY = Key.f8
STOP_KEY = Key.f9

MODS = {
    Key.ctrl: "ctrl", Key.ctrl_l: "ctrl", Key.ctrl_r: "ctrl",
    Key.alt: "alt", Key.alt_l: "alt", Key.alt_r: "alt", Key.alt_gr: "alt",
    Key.cmd: "win", Key.cmd_r: "win",
}

# shift is tracked separately: alone it just capitalizes text, but combined with
# ctrl/alt/win it belongs in the recorded shortcut (e.g. ctrl+shift+t)
SHIFT_KEYS = {k for k in (getattr(Key, n, None) for n in ("shift", "shift_l", "shift_r")) if k}

SPECIAL = {
    Key.enter: "enter", Key.tab: "tab", Key.esc: "esc", Key.backspace: "backspace",
    Key.delete: "delete", Key.insert: "insert", Key.home: "home", Key.end: "end",
    Key.page_up: "pageup", Key.page_down: "pagedown", Key.caps_lock: "capslock",
    Key.up: "up", Key.down: "down", Key.left: "left", Key.right: "right",
    Key.f1: "f1", Key.f2: "f2", Key.f3: "f3", Key.f4: "f4", Key.f5: "f5",
    Key.f6: "f6", Key.f7: "f7", Key.f10: "f10", Key.f11: "f11", Key.f12: "f12",
}


# ---------------------------------------------------------------- recorder

class Recorder:
    def __init__(self, ignore=None):
        self.steps = []
        self.checkpoint = 0          # index steps before this are confirmed
        self.paused = False
        self.stop_requested = False
        self.checkpoint_requested = False
        self.redo_requested = False  # F7 while paused at a checkpoint
        self._ignore = ignore        # fn(x, y) -> True to skip clicks on our own UI
        self._last = time.monotonic()
        self._text = ""
        self._text_delay = 0.0
        self._press = None           # pending mouse-down: (x, y, button, t0, delay)
        self._mods = set()
        self._shift = False
        self._win_candidate = False   # a Windows key held with nothing else -> lone Win press
        self._mouse = mouse.Listener(on_click=self._on_click, on_scroll=self._on_scroll)
        self._kbd = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)

    def start(self):
        self._last = time.monotonic()
        self._mouse.start()
        self._kbd.start()

    def stop(self):
        self._flush()
        self._mouse.stop()
        self._kbd.stop()

    def resume(self):
        self.paused = False
        self.checkpoint_requested = False
        self.redo_requested = False
        self._last = time.monotonic()  # don't replay the time spent in the dialog

    def mark_checkpoint(self):
        self.checkpoint = len(self.steps)

    def undo_last(self):
        if len(self.steps) > self.checkpoint:
            self.steps.pop()
            return True
        return False

    def _delay(self):
        now = time.monotonic()
        d = round(now - self._last, 3)
        self._last = now
        return d

    def _flush(self):
        if self._text:
            self.steps.append({"t": "text", "text": self._text, "delay": self._text_delay})
            self._text = ""

    def _on_click(self, x, y, button, pressed):
        if self.paused:
            self._press = None
            return
        if pressed:
            if self._ignore and self._ignore(x, y):
                self._press = None
                return
            self._flush()
            self._win_candidate = False
            self._press = (x, y, button, time.monotonic(), self._delay())
            return
        # release: far from the press point -> it was a drag, else a click
        p, self._press = self._press, None
        if p is None:
            return
        x1, y1, btn, t0, delay = p
        if (x - x1) ** 2 + (y - y1) ** 2 >= 100:  # moved 10px or more
            self.steps.append({"t": "drag", "x": x1, "y": y1, "x2": x, "y2": y,
                               "button": btn.name,
                               "dur": round(time.monotonic() - t0, 3), "delay": delay})
        else:
            self.steps.append({"t": "click", "x": x1, "y": y1,
                               "button": btn.name, "delay": delay})
        self._last = time.monotonic()  # next delay counts from the release

    def _on_scroll(self, x, y, dx, dy):
        if self.paused:
            return
        self._win_candidate = False
        d = self._delay()
        last = self.steps[-1] if self.steps else None
        # coalesce a burst of wheel notches into one step
        if (not self._text and last and last["t"] == "scroll" and d < 0.3
                and (dy > 0) == (last["dy"] > 0)):
            last["dy"] += dy
            return
        self._flush()
        self.steps.append({"t": "scroll", "x": x, "y": y, "dy": dy, "delay": d})

    def _on_press(self, key):
        if key == STOP_KEY:
            self._flush()
            self.stop_requested = True
            return
        if key == CHECKPOINT_KEY:
            self._flush()
            self.checkpoint_requested = True
            return
        if self.paused:
            if key == Key.f7:
                self.redo_requested = True
            return
        if key in SHIFT_KEYS:
            self._shift = True
            return
        if key in MODS:
            # a Windows key pressed on its own (opens Start menu) is recorded on release;
            # if any other key/click follows, it's a combo instead and this clears
            self._win_candidate = MODS[key] == "win" and not self._mods and not self._shift
            self._mods.add(MODS[key])
            return
        ch = getattr(key, "char", None)
        if key == Key.space:
            ch = " "
        if self._mods:  # ctrl/alt/win held -> hotkey combo
            name = None
            if ch:
                name = chr(ord(ch) + 96) if ord(ch) < 32 else ch  # ctrl yields control chars
            elif key in SPECIAL:
                name = SPECIAL[key]
            if name:
                mods = sorted(self._mods) + (["shift"] if self._shift else [])
                self._flush()
                self.steps.append({"t": "hotkey", "keys": mods + [name],
                                   "delay": self._delay()})
                self._win_candidate = False
            return
        if ch and (ch.isprintable() or ch == " "):
            if not self._text:
                self._text_delay = self._delay()
            else:
                self._delay()
            self._text += ch
        elif key in SPECIAL:
            self._flush()
            if self._shift:  # Shift+Tab, Shift+arrows (selection), etc. keep the shift
                self.steps.append({"t": "hotkey", "keys": ["shift", SPECIAL[key]],
                                   "delay": self._delay()})
            else:
                self.steps.append({"t": "key", "key": SPECIAL[key], "delay": self._delay()})

    def _on_release(self, key):
        if key in MODS:
            self._mods.discard(MODS[key])
            if MODS[key] == "win" and self._win_candidate and not self.paused:
                self._win_candidate = False
                self._flush()
                self.steps.append({"t": "key", "key": "win", "delay": self._delay()})
        elif key in SHIFT_KEYS:
            self._shift = False


# ---------------------------------------------------------------- playback

def describe(step):
    t = step["t"]
    if t == "click":
        return f"{step['button']} click at ({step['x']}, {step['y']})"
    if t == "text":
        txt = step["text"] if len(step["text"]) <= 40 else step["text"][:37] + "..."
        return f'type "{txt}"'
    if t == "key":
        return f"press [{step['key']}]"
    if t == "hotkey":
        return "press [" + " + ".join(step["keys"]) + "]"
    if t == "scroll":
        return f"scroll {'up' if step['dy'] > 0 else 'down'} {abs(step['dy'])} at ({step['x']}, {step['y']})"
    if t == "drag":
        return (f"drag from ({step['x']}, {step['y']}) "
                f"to ({step['x2']}, {step['y2']})")
    if t == "path":
        dur = sum(p[2] for p in step["points"])
        return f"move pointer ({len(step['points'])} points, {dur:.1f}s)"
    return t


# ---------------------------------------------------------------- unicode typing
# pyautogui.write() only emits characters in its ASCII key table and silently drops
# everything else (accented letters, dashes, symbols). Replay typed text via Win32
# SendInput with KEYEVENTF_UNICODE instead, which reproduces any character regardless
# of keyboard layout. MOUSEINPUT is defined only so sizeof(INPUT) matches the real
# Win32 struct — SendInput rejects a wrong cbSize and silently types nothing.
from ctypes import wintypes


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def utf16_units(text):
    """UTF-16-LE code units for `text` (astral chars yield a surrogate pair)."""
    data = text.encode("utf-16-le")
    return struct.unpack(f"<{len(data) // 2}H", data)


def type_text(text, interval=0.02):
    """Type `text` as Unicode via SendInput; fall back to pyautogui on any failure."""
    try:
        user32 = ctypes.windll.user32
        UNICODE, KEYUP = 0x0004, 0x0002

        def emit(unit, flags):
            inp = _INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(0, unit, flags, 0, None)))
            if not user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)):
                raise OSError("SendInput rejected")

        for ch in text:
            for unit in utf16_units(ch):
                emit(unit, UNICODE)
                emit(unit, UNICODE | KEYUP)
            if interval:
                time.sleep(interval)
    except Exception:
        import pyautogui
        pyautogui.write(text, interval=interval)  # ASCII-only fallback


def play(steps, on_step=None, cancel=None):
    """Replay steps. Returns False if cancelled via the `cancel` callable, else True."""
    import pyautogui
    pyautogui.FAILSAFE = True

    # start from screen center so the first glide is consistent regardless of where the
    # cursor happened to be (matches the centered start used when recording)
    sw, sh = pyautogui.size()
    pyautogui.moveTo(sw // 2, sh // 2, _pause=False)

    def cancelled():
        return cancel is not None and cancel()

    def wait(secs):
        end = time.perf_counter() + secs
        while time.perf_counter() < end:
            if cancelled():
                return False
            time.sleep(min(0.1, max(0.0, end - time.perf_counter())))
        return True

    def glide(x2, y2, dur):
        # ~120 fps ease-out glide; pyautogui's own duration moves at ~20 fps and looks steppy
        x1, y1 = pyautogui.position()
        t0 = time.perf_counter()
        while True:
            if cancelled():
                return
            f = (time.perf_counter() - t0) / dur
            if f >= 1:
                break
            e = 1 - (1 - f) ** 3
            pyautogui.moveTo(round(x1 + (x2 - x1) * e), round(y1 + (y2 - y1) * e),
                             _pause=False)
            time.sleep(1 / 120)
        pyautogui.moveTo(x2, y2, _pause=False)

    for i, s in enumerate(steps):
        if cancelled():
            return False
        delay = s.get("delay", 0)
        if on_step:
            on_step(i)
        t = s["t"]
        if t in ("click", "scroll", "drag"):
            x, y = s["x"], s["y"]
            cx, cy = pyautogui.position()
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            dur = 0.0 if dist < 5 else min(0.6, max(0.15, dist / 2500))
            if not wait(max(0, delay - dur)):  # glide time counts toward the recorded delay
                return False
            if dur:
                glide(x, y, dur)  # animated slide to the target instead of teleporting
            if cancelled():
                return False
            if t == "click":
                pyautogui.click(x, y, button=s["button"])
            elif t == "drag":
                pyautogui.mouseDown(button=s["button"], _pause=False)
                time.sleep(0.05)
                glide(s["x2"], s["y2"], max(0.15, min(s.get("dur", 0.3), 2)))
                time.sleep(0.05)
                pyautogui.mouseUp(button=s["button"], _pause=False)
            else:
                # ponytail: 120 = one wheel notch on Windows; make configurable if a mouse/app disagrees
                pyautogui.scroll(int(s["dy"] * 120))
            continue
        if not wait(delay):
            return False
        if t == "text":
            type_text(s["text"])
        elif t == "key":
            pyautogui.press(s["key"])
        elif t == "hotkey":
            pyautogui.hotkey(*s["keys"])
        elif t == "path":  # legacy recordings with stored trajectories
            for x, y, dt in s["points"]:
                if cancelled():
                    return False
                if dt > 0:
                    time.sleep(dt)
                pyautogui.moveTo(x, y, _pause=False)
    return True


def load_recording(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_recording(name, steps):
    RECORDINGS_DIR.mkdir(exist_ok=True)
    path = RECORDINGS_DIR / f"{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"name": name, "created": datetime.now().isoformat(timespec="seconds"),
                   "steps": steps}, f, indent=1)
    return path


STARTUP_DIR = (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" /
               "Start Menu" / "Programs" / "Startup")


def startup_path(name):
    return STARTUP_DIR / f"Pointerizer - {name}.cmd"


def set_startup(name, enabled):
    """Create/remove a launcher in the user's Startup folder (runs at Windows sign-in)."""
    p = startup_path(name)
    if not enabled:
        p.unlink(missing_ok=True)
        return
    target = RECORDINGS_DIR / f"{name}.json"
    # --wait 300: give the desktop 5 minutes to settle after sign-in
    if getattr(sys, "frozen", False):
        cmd = f'start "" "{sys.executable}" --play "{target}" --wait 300'
    else:
        cmd = (f'start "" "{sys.executable}" "{Path(__file__).resolve()}"'
               f' --play "{target}" --wait 300')
    p.write_text("@echo off\n" + cmd + "\n", encoding="mbcs")


# ---------------------------------------------------------------- UI

STYLE = """
* { font-family: 'Segoe UI', sans-serif; }
QWidget { background: #212121; color: #ececec; font-size: 13px; }
QLabel { background: transparent; }
QLabel#title { font-size: 21px; font-weight: 600; }
QLabel#subtitle, QLabel#status { color: #9b9b9b; font-size: 12px; }
QListWidget { background: #181818; border: 1px solid #303030; border-radius: 12px;
              padding: 6px; outline: none; }
QWidget#flowrow { background: transparent; }
QLabel#section { color: #cfcfcf; font-size: 12px; font-weight: 600;
                 text-transform: uppercase; letter-spacing: 1px; }
QLabel#rowsub { color: #8a8a8a; font-size: 11px; }
QCheckBox#rowcheck { spacing: 0; }
QCheckBox#rowcheck::indicator { width: 17px; height: 17px; border: 1px solid #4a4a4a;
                                border-radius: 5px; background: #202020; }
QCheckBox#rowcheck::indicator:hover { border-color: #6e6e6e; }
QCheckBox#rowcheck::indicator:checked { background: #3b82f6; border-color: #3b82f6; }
QPushButton#trashbtn { background: #dc2626; border: none; border-radius: 7px; padding: 0; }
QPushButton#trashbtn:hover { background: #ef3b3b; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 2px 4px 0; }
QScrollBar::handle:vertical { background: #3a3a3a; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #4a4a4a; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QListWidget::item { padding: 2px 4px; border-radius: 8px; }
QListWidget::item:selected { background: #2e2e2e; color: #ffffff; }
QListWidget::item:hover:!selected { background: #232323; }
QLineEdit { background: #181818; border: 1px solid #3a3a3a; border-radius: 10px;
            padding: 9px 12px; selection-background-color: #4a4a4a; }
QLineEdit:focus { border-color: #6e6e6e; }
QPushButton { background: #2f2f2f; color: #ececec; border: 1px solid #3f3f3f;
              border-radius: 10px; padding: 9px 16px; font-weight: 600; }
QPushButton:hover { background: #3a3a3a; }
QPushButton:disabled { background: #262626; color: #6b6b6b; border-color: #2e2e2e; }
QPushButton#record { background: #dc2626; border: none; color: #ffffff; }
QPushButton#record:hover { background: #ef3b3b; }
QPushButton#primary { background: #ececec; border: none; color: #0d0d0d; }
QPushButton#primary:hover { background: #ffffff; }
QPushButton#accent { background: #f5c542; border: none; color: #0d0d0d; }
QPushButton#accent:hover { background: #ffd35c; }
QPushButton#rowbtn { background: transparent; border: none; border-radius: 6px; padding: 0; }
QPushButton#rowbtn:hover { background: #3a3a3a; }
QPushButton#daychip { background: transparent; border: 1px solid #3a3a3a; color: #9b9b9b;
                      padding: 6px 0; border-radius: 14px; font-size: 12px;
                      font-weight: 500; min-width: 0; }
QPushButton#daychip:hover { background: #2a2a2a; }
QPushButton#daychip:checked { background: #ececec; color: #0d0d0d;
                              border: 1px solid #ececec; }
QPushButton#pillbtn { background: #f5c542; color: #0d0d0d; border: none; }
QPushButton#pillbtn:hover { background: #ffd35c; }
QComboBox, QSpinBox, QTimeEdit { background: #181818; border: 1px solid #3a3a3a;
                                 border-radius: 8px; padding: 6px 10px; }
QComboBox:focus, QSpinBox:focus, QTimeEdit:focus { border-color: #6e6e6e; }
QComboBox::drop-down { background: transparent; border: none; width: 24px; }
QSpinBox::up-button, QSpinBox::down-button { background: transparent; border: none;
                                             width: 18px; }
QComboBox QAbstractItemView { background: #181818; border: 1px solid #3a3a3a;
                              selection-background-color: #343434; }
QCheckBox { color: #9b9b9b; spacing: 8px; }
QCheckBox:disabled { color: #565656; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #4a4a4a;
                       border-radius: 4px; background: #181818; }
QCheckBox::indicator:checked { background: #dc2626; border-color: #dc2626; }
QFrame#pill { background: #2b2b2b; border: 1px solid #4a4a4a; border-radius: 23px; }
QLabel#recdot { color: #f93a37; font-size: 15px; }
QPushButton#pillbtn { border-radius: 15px; padding: 7px 14px; }
QPushButton#pillfinish { background: #dc2626; color: #ffffff; border: none;
                         border-radius: 15px; padding: 7px 14px; }
QPushButton#pillfinish:hover { background: #ef3b3b; }
"""


def run_ui():
    from PySide6 import QtCore, QtGui, QtWidgets

    qapp = QtWidgets.QApplication(sys.argv)
    qapp.setStyleSheet(STYLE)
    icon = Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "icon.ico"
    if icon.exists():
        qapp.setWindowIcon(QtGui.QIcon(str(icon)))

    icon_font = ("Segoe Fluent Icons" if "Segoe Fluent Icons" in QtGui.QFontDatabase.families()
                 else "Segoe MDL2 Assets")

    ICON_FACTOR = 0.88  # glyph size vs box: these MDL2 glyphs nearly fill the em, so
    #                     leave a small margin to avoid horizontal clipping

    def fluent_icon(glyph, color="#ececec", size=15, bias=0.0):
        # Render the glyph into a pixmap that is TALLER than `size` by 2*bias, so the
        # downward `bias` shift (which aligns the icon with adjacent button text) has
        # headroom and never clips. iconSize is set to match, so nothing is squashed.
        dpr = qapp.devicePixelRatio() or 1
        S = max(1, round(size * dpr))
        D = round(bias * dpr)
        H = S + 2 * D
        pm = QtGui.QPixmap(S, H)
        pm.setDevicePixelRatio(dpr)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setRenderHint(QtGui.QPainter.TextAntialiasing)
        f = QtGui.QFont(icon_font)
        f.setPixelSize(max(1, round(S * ICON_FACTOR)))
        p.setFont(f)
        p.setPen(QtGui.QColor(color))
        # glyph centered in the box, then shifted down by D (rect origin at 2D)
        p.drawText(QtCore.QRectF(0, 2 * D, S, S),
                   QtCore.Qt.AlignCenter | QtCore.Qt.TextDontClip, glyph)
        p.end()
        return QtGui.QIcon(pm)

    TEXT_BIAS = 2.0  # nudge icons down to sit on the text midline (icon+text buttons)

    def set_btn_icon(btn, glyph, color="#ececec", size=15, with_text=True):
        bias = TEXT_BIAS if with_text else 0.0
        btn.setIcon(fluent_icon(glyph, color, size, bias))
        btn.setIconSize(QtCore.QSize(size, round(size + 2 * bias)))

    # Qt's built-in combo/spin arrows are black — invisible on our dark fields.
    # Render light chevrons to PNG once and point the stylesheet at them.
    # Everything stays next to the exe for portability; APPDATA only if read-only.
    ui_dir = BASE_DIR / "ui"
    try:
        ui_dir.mkdir(exist_ok=True)
        (ui_dir / "probe.tmp").write_text("x")
        (ui_dir / "probe.tmp").unlink()
    except OSError:
        ui_dir = Path(os.environ.get("APPDATA", str(BASE_DIR))) / "Pointerizer"
        ui_dir.mkdir(exist_ok=True)

    def glyph_png(fname, glyph, size=12, color="#b0b0b0"):
        path = ui_dir / fname
        pm = QtGui.QPixmap(size, size)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        f = QtGui.QFont(icon_font)
        f.setPixelSize(size)
        p.setFont(f)
        p.setPen(QtGui.QColor(color))
        p.drawText(QtCore.QRectF(0, 0, size, size), QtCore.Qt.AlignCenter, glyph)
        p.end()
        pm.save(str(path), "PNG")
        return str(path).replace("\\", "/")

    chev_down = glyph_png("chevron_down.png", chr(0xE70D))
    chev_up = glyph_png("chevron_up.png", chr(0xE70E))
    qapp.setStyleSheet(STYLE + f'''
QComboBox::down-arrow {{ image: url("{chev_down}"); width: 12px; height: 12px; }}
QSpinBox::down-arrow {{ image: url("{chev_down}"); width: 10px; height: 10px; }}
QSpinBox::up-arrow {{ image: url("{chev_up}"); width: 10px; height: 10px; }}
''')

    GLYPH_CHECK, GLYPH_REDO, GLYPH_STOP, GLYPH_CLOCK, GLYPH_FLAG = (
        chr(0xE73E), chr(0xE72C), chr(0xE71A), chr(0xE823), chr(0xE7C1))
    GLYPH_PLAY, GLYPH_RECORD, GLYPH_TRASH = chr(0xE768), chr(0xE7C8), chr(0xE74D)
    GLYPH_PENCIL = chr(0xE70F)

    OVERLAY_FLAGS = (QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint |
                     QtCore.Qt.Tool)

    class Border(QtWidgets.QWidget):
        """Colored frame around a screen; clicks pass straight through it."""
        def __init__(self, geo, color):
            super().__init__(None, OVERLAY_FLAGS | QtCore.Qt.WindowTransparentForInput)
            self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
            self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
            self.setGeometry(geo)
            self._color = color

        def set_color(self, color):
            self._color = color
            self.update()

        def paintEvent(self, _):
            p = QtGui.QPainter(self)
            p.setPen(QtGui.QPen(QtGui.QColor(self._color), 5))
            p.drawRect(self.rect().adjusted(2, 2, -3, -3))

    class Pill(QtWidgets.QWidget):
        """Floating, draggable recording control."""
        def __init__(self, on_checkpoint, on_finish, on_rect, on_dragged):
            super().__init__(None, OVERLAY_FLAGS)
            self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
            self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
            self._on_rect = on_rect
            self._on_dragged = on_dragged
            self._drag = None
            self._did_move = False
            frame = QtWidgets.QFrame(objectName="pill")
            lay = QtWidgets.QHBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(frame)
            h = QtWidgets.QHBoxLayout(frame)
            h.setContentsMargins(18, 9, 10, 9)
            h.setSpacing(10)
            self.dot = QtWidgets.QLabel("●", objectName="recdot")
            h.addWidget(self.dot)
            h.addWidget(QtWidgets.QLabel("Recording"))
            h.addSpacing(6)
            cp = QtWidgets.QPushButton("Checkpoint (F8)", objectName="pillbtn")
            set_btn_icon(cp, GLYPH_FLAG, "#0d0d0d")
            fin = QtWidgets.QPushButton("Finish (F9)", objectName="pillfinish")
            set_btn_icon(fin, GLYPH_STOP)
            cp.clicked.connect(on_checkpoint)
            fin.clicked.connect(on_finish)
            h.addWidget(cp)
            h.addWidget(fin)

        def _report(self):
            g = self.frameGeometry()
            self._on_rect((g.left(), g.top(), g.right(), g.bottom()))

        def moveEvent(self, _): self._report()
        def resizeEvent(self, _): self._report()
        def showEvent(self, _): self._report()
        def hideEvent(self, _): self._on_rect(None)

        def mousePressEvent(self, e):
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._did_move = False

        def mouseMoveEvent(self, e):
            if self._drag is not None:
                self._did_move = True
                self.move(e.globalPosition().toPoint() - self._drag)

        def mouseReleaseEvent(self, _):
            self._drag = None
            if self._did_move:
                self._did_move = False
                self._on_dragged()  # dropping the pill pauses at a checkpoint

    win = QtWidgets.QWidget()
    win.setWindowTitle("Pointerizer")
    win.resize(460, 540)

    layout = QtWidgets.QVBoxLayout(win)
    layout.setContentsMargins(24, 22, 24, 16)
    layout.setSpacing(8)

    title = QtWidgets.QLabel("Pointerizer", objectName="title")
    subtitle = QtWidgets.QLabel("Record once. Replay forever.", objectName="subtitle")
    layout.addWidget(title)
    layout.addWidget(subtitle)
    layout.addSpacing(10)

    header = QtWidgets.QHBoxLayout()
    myflows = QtWidgets.QLabel("My Flows", objectName="section")
    header.addWidget(myflows)
    header.addStretch(1)
    del_icon = QtWidgets.QPushButton(objectName="trashbtn")  # shown only when flows selected
    set_btn_icon(del_icon, GLYPH_TRASH, "#ffffff", 15, with_text=False)
    del_icon.setFixedSize(30, 26)
    del_icon.setCursor(QtCore.Qt.PointingHandCursor)
    sp = del_icon.sizePolicy()
    sp.setRetainSizeWhenHidden(True)  # keep its slot so the list doesn't shift when it toggles
    del_icon.setSizePolicy(sp)
    del_icon.setVisible(False)
    header.addWidget(del_icon)
    layout.addLayout(header)

    listw = QtWidgets.QListWidget()
    listw.setSpacing(5)  # gap between flow rows
    # SingleSelection: a row-click highlights one flow (the Play/rename/schedule target),
    # no drag multi-select. Ticking flows for deletion is done via their checkbox (cb_clicked).
    listw.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
    layout.addWidget(listw, stretch=1)

    startup_cb = QtWidgets.QCheckBox("Run when I sign in to Windows")
    startup_cb.setEnabled(False)
    layout.addWidget(startup_cb)

    btn_row = QtWidgets.QHBoxLayout()
    play_btn = QtWidgets.QPushButton("Play", objectName="primary")
    set_btn_icon(play_btn, GLYPH_PLAY, "#0d0d0d")
    record_btn = QtWidgets.QPushButton("Record (F9)", objectName="record")
    set_btn_icon(record_btn, GLYPH_RECORD)
    for b in (play_btn, record_btn):
        b.setFixedHeight(40)
        btn_row.addWidget(b, stretch=1)
    layout.addLayout(btn_row)

    status = QtWidgets.QLabel("", objectName="status", alignment=QtCore.Qt.AlignCenter)
    layout.addWidget(status)

    state = {"recorder": None, "name": "", "play_result": None, "pill_rect": None}
    overlays = []

    def show_overlays(color):
        for s in qapp.screens():
            b = Border(s.geometry(), color)
            b.show()
            overlays.append(b)

    def hide_overlays():
        for b in overlays:
            b.close()
        overlays.clear()

    def in_own_ui(x, y):
        r = state["pill_rect"]
        return bool(r and r[0] - 4 <= x <= r[2] + 4 and r[1] - 4 <= y <= r[3] + 4)

    def request_checkpoint():
        if state["recorder"]:
            state["recorder"].checkpoint_requested = True

    def request_stop():
        if state["recorder"]:
            state["recorder"].stop_requested = True

    def pill_dragged():
        rec = state["recorder"]
        if not rec:
            return
        if rec.steps or rec._text:
            # mid-flow: pause at a checkpoint; Continue (F8) resets the clock
            rec.checkpoint_requested = True
        else:
            # nothing recorded yet: just restart the clock, no pause needed
            rec.resume()

    pill = Pill(request_checkpoint, request_stop,
                lambda r: state.__setitem__("pill_rect", r),
                on_dragged=pill_dragged)

    blink = QtCore.QTimer(interval=600)
    blink.timeout.connect(
        lambda: pill.dot.setStyleSheet("" if pill.dot.styleSheet() else "color: #5a2624;"))

    checked = set()        # names of selected flows (shown by their ticked checkbox)
    row_checks = {}        # name -> its QCheckBox (read-only indicator, rebuilt each refresh)
    anchor = {"name": None}  # last plainly-clicked row; Shift+click selects the range to it

    def select_by_name(name):
        for i in range(listw.count()):
            if listw.item(i).data(QtCore.Qt.UserRole) == name:
                listw.setCurrentRow(i)
                return

    def sync_row_checks():
        for nm, cb in row_checks.items():
            cb.setChecked(nm in checked)  # programmatic; does not fire `clicked`

    def update_del_bar():
        n = len(checked)
        del_icon.setVisible(n > 0)
        del_icon.setToolTip(f"Delete {n} selected" if n else "")

    def cb_clicked(name):
        # clicking a checkbox toggles that flow; Shift+click adds the range from the anchor
        mods = qapp.keyboardModifiers()
        names = [listw.item(i).data(QtCore.Qt.UserRole) for i in range(listw.count())]
        if mods & QtCore.Qt.ShiftModifier and anchor["name"] in names:
            a, b = sorted((names.index(anchor["name"]), names.index(name)))
            checked.update(names[a:b + 1])
        else:
            checked.discard(name) if name in checked else checked.add(name)
            anchor["name"] = name
        sync_row_checks()
        update_del_bar()

    def make_row(name):
        # transparent so the rounded hover/selection highlight shows through
        w = QtWidgets.QWidget(objectName="flowrow")
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(8, 0, 4, 0)
        h.setSpacing(6)

        cb = QtWidgets.QCheckBox(objectName="rowcheck")
        cb.setChecked(name in checked)
        cb.setFocusPolicy(QtCore.Qt.NoFocus)
        cb.clicked.connect(lambda _checked, n=name: cb_clicked(n))  # tick = select for delete
        row_checks[name] = cb
        h.addWidget(cb, 0, QtCore.Qt.AlignVCenter)

        texts = QtWidgets.QVBoxLayout()
        texts.setSpacing(1)
        texts.addStretch(1)  # vertically center the text block in the row
        name_lbl = QtWidgets.QLabel(name)
        # clicks on the text fall through to the row so a row-click highlights it (Play target)
        name_lbl.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        texts.addWidget(name_lbl)
        try:
            sched = load_recording(RECORDINGS_DIR / f"{name}.json").get("schedule")
        except Exception:
            sched = None
        bits = []
        ic = f'<span style="font-family:{icon_font}">{{}}</span>'
        if sched:
            bits.append(ic.format(GLYPH_CLOCK) + " " + sched)
        if startup_path(name).exists():
            bits.append(ic.format(chr(0xE7E8)) + " sign-in")
        if bits:
            sub = QtWidgets.QLabel("&nbsp;&nbsp;&nbsp;".join(bits), objectName="rowsub",
                                   textFormat=QtCore.Qt.RichText)
            sub.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
            texts.addWidget(sub)
        texts.addStretch(1)
        h.addLayout(texts)
        h.addStretch(1)

        for glyph, tip, fn in ((GLYPH_PENCIL, "Rename", rename_flow),
                               (GLYPH_CLOCK, "Schedule", show_schedule)):
            b = QtWidgets.QPushButton(objectName="rowbtn", toolTip=tip)
            set_btn_icon(b, glyph, "#9b9b9b", 13, with_text=False)
            b.setFixedSize(28, 28)
            b.clicked.connect(lambda _=False, f=fn, n=name: (select_by_name(n), f(n)))
            h.addWidget(b)
        return w

    def refresh():
        listw.clear()
        row_checks.clear()
        RECORDINGS_DIR.mkdir(exist_ok=True)
        names = [p.stem for p in sorted(RECORDINGS_DIR.glob("*.json"))]
        checked.intersection_update(names)  # drop stale checks
        for name in names:
            item = QtWidgets.QListWidgetItem()
            item.setData(QtCore.Qt.UserRole, name)
            row = make_row(name)
            item.setSizeHint(QtCore.QSize(0, row.sizeHint().height() + 12))
            listw.addItem(item)
            listw.setItemWidget(item, row)
        update_del_bar()

    def selected_name():
        item = listw.currentItem()
        return item.data(QtCore.Qt.UserRole) if item else None

    def selected_path():
        n = selected_name()
        return RECORDINGS_DIR / f"{n}.json" if n else None

    def set_busy(busy):
        for b in (record_btn, play_btn):
            b.setEnabled(not busy)

    # ---- checkpoint dialog ----
    def show_checkpoint(rec):
        dlg = QtWidgets.QDialog(win)
        dlg.setWindowTitle("Checkpoint")
        dlg.resize(430, 380)
        dlg.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint)
        dlg.setWindowFlag(QtCore.Qt.WindowCloseButtonHint, False)  # force a choice
        v = QtWidgets.QVBoxLayout(dlg)
        new = rec.steps[rec.checkpoint:]
        head = QtWidgets.QLabel(f"{len(new)} action(s) since last checkpoint")
        head.setStyleSheet("font-size: 15px; font-weight: 600;")
        v.addWidget(head)
        box = QtWidgets.QListWidget()
        for s in new:
            box.addItem(describe(s))
        v.addWidget(box, stretch=1)
        row = QtWidgets.QHBoxLayout()
        cont = QtWidgets.QPushButton("Continue (F8)", objectName="accent")
        set_btn_icon(cont, chr(0xE72A), "#0d0d0d")  # forward arrow reads better than the check
        redo = QtWidgets.QPushButton(" Redo (F7)")  # leading space = icon-text breathing room
        set_btn_icon(redo, GLYPH_REDO, size=13)  # the circular glyph runs optically large
        stop = QtWidgets.QPushButton("Stop && save (F9)", objectName="record")
        set_btn_icon(stop, GLYPH_STOP)

        def do_redo():
            # remove only the most recent action; press again to peel more.
            # rebuild the list straight from the model so the view can't drift.
            rec.undo_last()
            box.clear()
            for s in rec.steps[rec.checkpoint:]:
                box.addItem(describe(s))
            head.setText(f"{box.count()} action(s) kept — paused until you Continue (F8)")

        cont.clicked.connect(lambda: dlg.done(1))
        redo.clicked.connect(do_redo)
        stop.clicked.connect(lambda: dlg.done(3))
        for b in (cont, redo, stop):
            row.addWidget(b)
        v.addLayout(row)

        # F7/F8/F9 are driven ONLY by the recorder's global listener (works whether
        # or not this dialog is focused) — no QShortcut, so keys never double-fire.
        rec.checkpoint_requested = False  # a fresh F8 press now means Continue
        rec.redo_requested = False
        watch = QtCore.QTimer(dlg, interval=80)

        def on_watch():
            if rec.stop_requested:
                dlg.done(3)
            elif rec.checkpoint_requested:
                dlg.done(1)
            elif rec.redo_requested:
                rec.redo_requested = False
                do_redo()

        watch.timeout.connect(on_watch)
        watch.start()
        # parent window is hidden while recording; sit just above the pill at the bottom
        scr = win.screen().geometry()
        dlg.move(scr.center().x() - dlg.width() // 2,
                 scr.bottom() - dlg.height() - 120)
        return dlg.exec()

    # ---- record flow ----
    poll = QtCore.QTimer(interval=100)

    def prompt_name(default):
        """Ask for a name after recording. Enter accepts `default`; Discard returns None."""
        dlg = QtWidgets.QDialog(win)
        dlg.setWindowTitle("Save recording")
        dlg.setFixedWidth(420)
        dlg.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint)
        v = QtWidgets.QVBoxLayout(dlg)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(12)
        head = QtWidgets.QLabel("Name this recording")
        head.setStyleSheet("font-size: 15px; font-weight: 600;")
        v.addWidget(head)
        edit = QtWidgets.QLineEdit(default)
        edit.selectAll()  # Enter keeps the default; typing replaces it
        v.addWidget(edit)
        row = QtWidgets.QHBoxLayout()
        okb = QtWidgets.QPushButton("Save", objectName="primary")
        cancelb = QtWidgets.QPushButton("Discard")
        okb.clicked.connect(dlg.accept)
        cancelb.clicked.connect(dlg.reject)
        edit.returnPressed.connect(dlg.accept)
        row.addWidget(okb, stretch=1)
        row.addWidget(cancelb, stretch=1)
        v.addLayout(row)
        edit.setFocus()
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return None
        return re.sub(r"[^\w\- ]", "", edit.text().strip()) or default

    def unique_name(name):
        if not (RECORDINGS_DIR / f"{name}.json").exists():
            return name
        i = 2
        while (RECORDINGS_DIR / f"{name} ({i}).json").exists():
            i += 1
        return f"{name} ({i})"

    def start_recording():
        state["recorder"] = Recorder(ignore=in_own_ui)
        set_busy(True)
        status.setText("Recording...  F8 checkpoint  ·  F9 finish")
        win.setEnabled(False)  # block interactions with the app while recording
        win.hide()
        show_overlays("#f93a37")
        scr = win.screen().geometry()  # the screen the app lives on
        pill.adjustSize()
        pill.move(scr.center().x() - pill.width() // 2,
                  scr.bottom() - pill.height() - 48)
        pill.show()
        blink.start()
        # center the cursor before capture starts, so recordings begin from a known point
        QtGui.QCursor.setPos(scr.center())
        state["recorder"].start()
        poll.start()

    def finish_recording():
        poll.stop()
        blink.stop()
        pill.hide()
        hide_overlays()
        rec = state["recorder"]
        state["recorder"] = None
        rec.stop()
        f9_hit["v"] = False  # the F9 that finished us must not start a new recording
        win.setEnabled(True)
        win.show()
        win.raise_()
        win.activateWindow()
        set_busy(False)
        if rec.steps:
            default = datetime.now().strftime("Recording %Y-%m-%d %H-%M-%S")
            name = prompt_name(default)
            if name:
                name = unique_name(name)
                save_recording(name, rec.steps)
                status.setText(f"Saved '{name}' ({len(rec.steps)} steps)")
            else:
                status.setText("Recording discarded.")
        else:
            status.setText("Nothing recorded.")
        refresh()

    def on_poll():
        rec = state["recorder"]
        if rec is None:
            poll.stop()
            return
        if rec.stop_requested:
            finish_recording()
            return
        if rec.checkpoint_requested and not rec.paused:
            rec.paused = True
            poll.stop()
            for b in overlays:
                b.set_color("#f5c542")  # yellow while paused at a checkpoint
            choice = show_checkpoint(rec)
            if choice == 3:
                finish_recording()
            else:  # Continue (or Esc) — keep going from here
                for b in overlays:
                    b.set_color("#f93a37")
                rec.mark_checkpoint()
                rec.resume()
                poll.start()

    poll.timeout.connect(on_poll)

    # ---- play flow ----
    play_poll = QtCore.QTimer(interval=200)

    def start_playback():
        path = selected_path()
        if not path:
            status.setText("Select a recording first.")
            return
        data = load_recording(path)
        set_busy(True)
        win.hide()
        show_overlays("#8e8ea0")

        def worker():
            esc = {"v": False}
            lst = keyboard.Listener(
                on_press=lambda k: esc.__setitem__("v", True) if k == Key.esc else None)
            lst.start()
            time.sleep(2)  # give the user's target window time to be in front
            try:
                if play(data["steps"], cancel=lambda: esc["v"]):
                    state["play_result"] = f"Played '{data['name']}' ({len(data['steps'])} steps)"
                else:
                    state["play_result"] = "Playback cancelled (Esc)"
            except Exception as e:
                state["play_result"] = f"Playback aborted: {e}"
            finally:
                lst.stop()

        state["play_result"] = None
        threading.Thread(target=worker, daemon=True).start()
        play_poll.start()

    def on_play_poll():
        if state["play_result"] is None:
            return
        play_poll.stop()
        hide_overlays()
        win.show()
        win.raise_()
        win.activateWindow()
        set_busy(False)
        status.setText(state["play_result"])

    play_poll.timeout.connect(on_play_poll)

    def remove_flow(name):
        set_startup(name, False)  # remove its sign-in launcher
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", f"Pointerizer - {name}"],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        (RECORDINGS_DIR / f"{name}.json").unlink(missing_ok=True)

    def confirm_delete(names):
        if not names:
            return
        m = QtWidgets.QMessageBox(win)
        m.setWindowTitle("Delete recording" + ("s" if len(names) > 1 else ""))
        if len(names) == 1:
            m.setText(f"Delete '{names[0]}'? This can't be undone.")
        else:
            m.setText(f"Delete these {len(names)} flows? This can't be undone.")
            m.setInformativeText("\n".join(f"• {n}" for n in names))
        m.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel)
        m.setDefaultButton(QtWidgets.QMessageBox.Yes)  # Enter confirms
        if m.exec() == QtWidgets.QMessageBox.Yes:
            for n in names:
                remove_flow(n)
            checked.clear()
            refresh()

    def delete_flow(name):
        confirm_delete([name])

    def delete_checked():
        confirm_delete(sorted(checked))

    def delete_selected():
        if checked:
            delete_checked()
        elif selected_name():
            delete_flow(selected_name())

    def rename_flow(name):
        dlg = QtWidgets.QDialog(win)
        dlg.setWindowTitle("Rename")
        dlg.setFixedWidth(420)
        v = QtWidgets.QVBoxLayout(dlg)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(12)
        head = QtWidgets.QLabel("New name")
        head.setStyleSheet("font-size: 15px; font-weight: 600;")
        v.addWidget(head)
        edit = QtWidgets.QLineEdit(name)
        edit.selectAll()
        v.addWidget(edit)
        row = QtWidgets.QHBoxLayout()
        okb = QtWidgets.QPushButton("Rename", objectName="primary")
        cancelb = QtWidgets.QPushButton("Cancel")
        okb.clicked.connect(dlg.accept)
        cancelb.clicked.connect(dlg.reject)
        edit.returnPressed.connect(dlg.accept)
        row.addWidget(okb, stretch=1)
        row.addWidget(cancelb, stretch=1)
        v.addLayout(row)
        edit.setFocus()
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        new = re.sub(r"[^\w\- ]", "", edit.text().strip())
        if not new or new == name:
            return
        new_p = RECORDINGS_DIR / f"{new}.json"
        if new_p.exists():
            status.setText(f"'{new}' already exists.")
            return
        data = load_recording(RECORDINGS_DIR / f"{name}.json")
        data["name"] = new
        data.pop("schedule", None)  # its Task Scheduler task is dropped below
        with open(new_p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        (RECORDINGS_DIR / f"{name}.json").unlink()
        if startup_path(name).exists():  # migrate the sign-in launcher
            set_startup(name, False)
            set_startup(new, True)
        # a scheduled task points at the old file; drop it rather than fire a broken one
        r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", f"Pointerizer - {name}"],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode == 0:
            status.setText(f"Renamed to '{new}' — its schedule was removed, re-create it.")
        else:
            status.setText(f"Renamed to '{new}'")
        refresh()
        select_by_name(new)

    def sync_startup(*_):
        n = selected_name()
        startup_cb.blockSignals(True)
        startup_cb.setEnabled(n is not None)
        startup_cb.setChecked(bool(n) and startup_path(n).exists())
        startup_cb.blockSignals(False)

    def toggle_startup(on):
        n = selected_name()
        if not n:
            return
        if on:
            # only one flow may run at sign-in — two would fight over the mouse
            for f in STARTUP_DIR.glob("Pointerizer - *.cmd"):
                old = f.stem[len("Pointerizer - "):]
                if old != n:
                    f.unlink()
                    status.setText(f"Sign-in run moved from '{old}' to '{n}'")
        set_startup(n, on)
        refresh()
        select_by_name(n)

    # ---- schedule dialog (wraps Windows Task Scheduler via schtasks) ----
    NO_WINDOW = subprocess.CREATE_NO_WINDOW

    def set_flow_schedule(name, summary, sched=None):
        # cache display string + structured schedule in the flow's JSON
        p = RECORDINGS_DIR / f"{name}.json"
        data = load_recording(p)
        if summary:
            data["schedule"] = summary
            data["sched"] = sched
        else:
            data.pop("schedule", None)
            data.pop("sched", None)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)

    GAP_MIN = 5  # required spacing between two flows' start times

    def find_schedule_conflict(name, freq, st, days):
        """Another flow whose schedule starts within GAP_MIN minutes, or None."""
        def close(t1, t2, period):  # circular distance under GAP_MIN
            d = abs(t1 - t2) % period
            return min(d, period - d) < GAP_MIN

        h, m = int(st[:2]), int(st[3:])
        for p in RECORDINGS_DIR.glob("*.json"):
            if p.stem == name:
                continue
            try:
                sc = load_recording(p).get("sched")
            except Exception:
                continue
            if not sc:
                continue
            oh, om = int(sc["time"][:2]), int(sc["time"][3:])
            if "Hourly" in (freq, sc["freq"]):
                # an hourly flow recurs every hour, so only minutes-past-hour matter
                if close(m, om, 60):
                    return p.stem
                continue
            if not close(h * 60 + m, oh * 60 + om, 1440):  # minute-of-day
                continue
            if "Daily" in (freq, sc["freq"]):  # daily overlaps any day
                return p.stem
            if set(days) & set(sc.get("days", [])):  # both weekly: shared day
                return p.stem
        return None

    def show_schedule(name):
        path = RECORDINGS_DIR / f"{name}.json"
        task = f"Pointerizer - {name}"
        dlg = QtWidgets.QDialog(win)
        dlg.setWindowTitle("Schedule")
        dlg.setFixedWidth(440)
        v = QtWidgets.QVBoxLayout(dlg)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(12)
        head = QtWidgets.QLabel(f"Schedule '{name}'")
        head.setStyleSheet("font-size: 15px; font-weight: 600;")
        v.addWidget(head)
        form = QtWidgets.QFormLayout()
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        freq = QtWidgets.QComboBox()
        freq.addItems(["Daily", "Hourly", "Weekly"])
        now = QtCore.QTime.currentTime().addSecs(600)
        hour = QtWidgets.QComboBox()
        hour.addItems([f"{h:02d}" for h in range(24)])
        hour.setCurrentText(f"{now.hour():02d}")
        hour.setFixedWidth(72)
        minute = QtWidgets.QComboBox()
        minute.addItems([f"{m:02d}" for m in range(0, 60, 5)])
        minute.setCurrentText(f"{now.minute() // 5 * 5:02d}")
        minute.setFixedWidth(72)
        trow = QtWidgets.QHBoxLayout()
        colon = QtWidgets.QLabel(":")
        colon.setStyleSheet("font-weight: 700; font-size: 16px; color: #ececec; padding: 0 3px;")
        trow.addWidget(hour)
        trow.addWidget(colon)
        trow.addWidget(minute)
        trow.addStretch(1)
        repeat = QtWidgets.QSpinBox(minimum=1, maximum=100, value=1)
        repeat.setFixedWidth(90)
        form.addRow("Frequency", freq)
        form.addRow("Start time", trow)
        form.addRow("Plays per run", repeat)
        # weekly day picker: toggle chips, shown only for Weekly
        days_w = QtWidgets.QWidget()
        dh = QtWidgets.QHBoxLayout(days_w)
        dh.setContentsMargins(0, 0, 0, 0)
        dh.setSpacing(4)
        day_btns = []
        for lbl, code in (("Mon", "MON"), ("Tue", "TUE"), ("Wed", "WED"), ("Thu", "THU"),
                          ("Fri", "FRI"), ("Sat", "SAT"), ("Sun", "SUN")):
            b = QtWidgets.QPushButton(lbl, objectName="daychip", checkable=True)
            day_btns.append((b, code))
            dh.addWidget(b, stretch=1)
        day_btns[datetime.now().weekday()][0].setChecked(True)
        form.addRow(days_w)  # span the full dialog width so chips never overflow
        form.setRowVisible(days_w, False)
        freq.currentTextChanged.connect(lambda t: form.setRowVisible(days_w, t == "Weekly"))
        v.addLayout(form)
        info = QtWidgets.QLabel("", objectName="status", wordWrap=True)
        v.addWidget(info)
        exists = subprocess.run(["schtasks", "/Query", "/TN", task], capture_output=True,
                                creationflags=NO_WINDOW).returncode == 0
        if exists:
            info.setText("Already scheduled — creating again replaces the existing schedule.")
        row = QtWidgets.QHBoxLayout()
        create = QtWidgets.QPushButton("Create schedule", objectName="primary")
        set_btn_icon(create, GLYPH_CHECK, "#0d0d0d")
        remove = QtWidgets.QPushButton("Remove schedule", enabled=exists)
        row.addWidget(create, stretch=1)
        row.addWidget(remove, stretch=1)
        v.addLayout(row)

        def do_create():
            fr = freq.currentText()
            st = f"{hour.currentText()}:{minute.currentText()}"
            days = [c for b, c in day_btns if b.isChecked()] if fr == "Weekly" else []
            if fr == "Weekly" and not days:
                info.setText("Pick at least one day of the week.")
                return
            other = find_schedule_conflict(name, fr, st, days)
            if other:
                info.setText(f"Too close to '{other}' — keep schedules at least "
                             f"{GAP_MIN} minutes apart. Pick a different time.")
                return
            target = f'--play "{path}" --repeat {repeat.value()}'
            if getattr(sys, "frozen", False):
                tr = f'"{sys.executable}" {target}'
            else:
                tr = f'"{sys.executable}" "{Path(__file__).resolve()}" {target}'
            args = ["schtasks", "/Create", "/F", "/TN", task, "/TR", tr,
                    "/SC", fr.upper(), "/ST", st]
            if days:
                args += ["/D", ",".join(days)]
            r = subprocess.run(args, capture_output=True, text=True, creationflags=NO_WINDOW)
            if r.returncode == 0:
                summary = f"{fr} · {st}"
                if fr == "Weekly":
                    picked = [b.text() for b, c in day_btns if b.isChecked()]
                    if picked:
                        summary = f"{', '.join(picked)} · {st}"
                if repeat.value() > 1:
                    summary += f" · ×{repeat.value()}"
                set_flow_schedule(name, summary, {"freq": fr, "time": st, "days": days})
                status.setText(f"Scheduled '{name}': {summary}")
                refresh()
                select_by_name(name)
                dlg.accept()
            else:
                info.setText((r.stderr or r.stdout).strip()[:300])

        def do_remove():
            subprocess.run(["schtasks", "/Delete", "/F", "/TN", task],
                           capture_output=True, creationflags=NO_WINDOW)
            set_flow_schedule(name, None)
            status.setText(f"Schedule removed for '{name}'")
            refresh()
            select_by_name(name)
            dlg.accept()

        create.clicked.connect(do_create)
        remove.clicked.connect(do_remove)
        create.setDefault(True)
        for seq in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            QtGui.QShortcut(QtGui.QKeySequence(seq), dlg, activated=do_create)
        # Delete key removes the schedule (only meaningful when one exists)
        QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Delete), dlg,
                        activated=lambda: do_remove() if exists else None)
        dlg.exec()

    record_btn.clicked.connect(start_recording)
    play_btn.clicked.connect(start_playback)
    del_icon.clicked.connect(delete_checked)
    listw.currentItemChanged.connect(sync_startup)
    listw.itemDoubleClicked.connect(lambda _it: start_playback())
    startup_cb.toggled.connect(toggle_startup)

    del_sc = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Delete), listw,
                             activated=delete_selected)
    del_sc.setContext(QtCore.Qt.WidgetShortcut)  # only while the list has focus

    # F9 while idle starts a recording (the Recorder's own listener handles F9-to-finish)
    f9_hit = {"v": False}
    idle_listener = keyboard.Listener(
        on_press=lambda k: f9_hit.__setitem__("v", True) if k == STOP_KEY else None)
    idle_listener.start()
    hot = QtCore.QTimer(interval=150)

    def on_hot():
        if f9_hit["v"]:
            f9_hit["v"] = False
            if record_btn.isEnabled() and win.isVisible():
                start_recording()

    hot.timeout.connect(on_hot)
    hot.start()

    refresh()
    win.show()
    qapp.exec()


# ---------------------------------------------------------------- entry

def selfcheck():
    steps = [
        {"t": "click", "x": 10, "y": 20, "button": "left", "delay": 0.1},
        {"t": "text", "text": "hello world", "delay": 0.2},
        {"t": "key", "key": "enter", "delay": 0.1},
        {"t": "hotkey", "keys": ["ctrl", "c"], "delay": 0.1},
        {"t": "hotkey", "keys": ["ctrl", "shift", "t"], "delay": 0.1},
        {"t": "hotkey", "keys": ["shift", "tab"], "delay": 0.1},
        {"t": "key", "key": "win", "delay": 0.1},
        {"t": "text", "text": "coração — não, café", "delay": 0.2},
        {"t": "click", "x": 3, "y": 4, "button": "right", "delay": 0.1},
        {"t": "click", "x": 3, "y": 4, "button": "middle", "delay": 0.1},
        {"t": "scroll", "x": 5, "y": 5, "dy": -3, "delay": 0.1},
        {"t": "path", "points": [[1, 2, 0], [30, 40, 0.05], [60, 80, 0.04]], "delay": 0.1},
        {"t": "drag", "x": 10, "y": 10, "x2": 200, "y2": 220, "button": "left",
         "dur": 0.5, "delay": 0.1},
    ]
    path = save_recording("_selfcheck", steps)
    loaded = load_recording(path)
    assert loaded["steps"] == steps
    for s in steps:
        assert describe(s)
    import pyautogui
    for key in list(SPECIAL.values()) + ["ctrl", "alt", "win", "shift"]:
        assert key in pyautogui.KEYBOARD_KEYS, key
    for btn in ("left", "right", "middle"):
        assert btn in ("left", "middle", "right")
    # unicode typing: UTF-16 decomposition + INPUT layout must match Win32, or
    # SendInput silently types nothing (a wrong cbSize is rejected without error).
    assert utf16_units("A") == (0x41,)
    assert utf16_units("ç") == (0xE7,)
    assert len(utf16_units("😀")) == 2  # astral char -> surrogate pair
    assert ctypes.sizeof(_INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    # a lone Windows key press is recorded (opens Start menu on replay); Win+D stays a hotkey
    from pynput.keyboard import KeyCode
    r = Recorder()
    r._on_press(Key.cmd); r._on_release(Key.cmd)
    assert [(s["t"], s.get("key")) for s in r.steps] == [("key", "win")]
    r = Recorder()
    r._on_press(Key.cmd); r._on_press(KeyCode.from_char("d")); r._on_release(Key.cmd)
    assert [s["t"] for s in r.steps] == ["hotkey"] and r.steps[0]["keys"] == ["win", "d"]
    path.unlink()
    print("selfcheck OK")


def main():
    ap = argparse.ArgumentParser(description="Pointerizer")
    ap.add_argument("--play", metavar="FILE", help="play a recording headless (for Task Scheduler)")
    ap.add_argument("--repeat", type=int, default=1, metavar="N",
                    help="play the recording N times back-to-back (with --play)")
    ap.add_argument("--wait", type=int, default=3, metavar="SEC",
                    help="seconds to wait before playback (desktop settle time)")
    ap.add_argument("--selfcheck", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.selfcheck:
        selfcheck()
    elif args.play:
        path = Path(args.play)
        if not path.exists():
            path = RECORDINGS_DIR / f"{args.play}.json"
        data = load_recording(path)
        esc = {"v": False}
        keyboard.Listener(
            on_press=lambda k: esc.__setitem__("v", True) if k == Key.esc else None).start()
        time.sleep(max(0, args.wait))  # grace period (desktop settle time)
        for _ in range(max(1, args.repeat)):
            if not play(data["steps"], cancel=lambda: esc["v"]):
                break
    else:
        run_ui()


if __name__ == "__main__":
    main()
