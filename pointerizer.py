"""Pointerizer — record mouse/keyboard actions, replay them, schedule via Task Scheduler.

Hotkeys while recording:  F8 = checkpoint (review/redo)   F9 = stop & save
Hotkeys while playing:    F7 = pause/resume               Esc = cancel
(the mouse slammed into any screen corner also cancels — pyautogui failsafe)
"""
import argparse
import contextlib
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
from pynput.keyboard import Key, KeyCode
from PySide6 import QtCore, QtGui, QtWidgets

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
RECORDINGS_DIR = BASE_DIR / "recordings"

CHECKPOINT_KEY = Key.f8
STOP_KEY = Key.f9
CANCEL_KEY = Key.esc  # stops playback outright
PAUSE_KEY = Key.f7    # holds playback where it is; press again to resume

FIXED_DELAY = 0.35  # gap used by flows saved without their timing — long enough that the
#                     previous action's UI has settled before the next one fires

# Grace period for a sign-in run, so the desktop finishes loading first. The sign-in
# launcher passes --signin rather than a number, so this value belongs to whichever
# build is installed: change it in a release and existing users pick it up, instead of
# being stuck with whatever was baked into their .cmd the day they ticked the box.
SIGNIN_WAIT = 60

MAPVK_VK_TO_CHAR = 2
DEAD_KEY_BIT = 0x80000000  # MapVirtualKey sets this for a dead key


def foreground_layout():
    """The keyboard layout of the window being typed into, not of our own thread.

    They differ as soon as someone has two layouts installed and switches: our listener
    runs on a background thread that keeps whatever layout it started with, so asking it
    about dead keys would consult the wrong keyboard.
    """
    u = ctypes.windll.user32
    tid = u.GetWindowThreadProcessId(u.GetForegroundWindow(), None)
    return u.GetKeyboardLayout(tid)


def dead_diacritic(key, ch):
    """`ch` if this key is a dead key that has a combining form, else None.

    MapVirtualKey is a read-only lookup — deliberately not ToUnicodeEx, which would
    consume the layout's pending dead-key state and corrupt what the user is typing.
    """
    vk = getattr(key, "vk", None)
    if not vk or not ch:
        return None
    fn = ctypes.windll.user32.MapVirtualKeyExW
    fn.restype = ctypes.c_uint
    if not fn(vk, MAPVK_VK_TO_CHAR, foreground_layout()) & DEAD_KEY_BIT:
        return None
    try:
        KeyCode.from_dead(ch)  # raises unless a COMBINING form exists for it
    except (KeyError, ValueError):
        return None
    return ch


NUMPAD_DIGITS = {0x60 + n: str(n) for n in range(10)}  # VK_NUMPAD0..9


# CP437 graphics for bytes 0x01–0x1F and 0x7F. Windows' Alt+N (no leading zero) types
# these for N in 1–31 (Alt+1 → ☺, Alt+26 → →) rather than the C0 control the codepage
# maps them to; they're identical across every OEM codepage, so this one table suffices.
OEM_CONTROL_GRAPHICS = {
    1: "☺", 2: "☻", 3: "♥", 4: "♦", 5: "♣", 6: "♠", 7: "•", 8: "◘", 9: "○",
    10: "◙", 11: "♂", 12: "♀", 13: "♪", 14: "♫", 15: "☼", 16: "►", 17: "◄",
    18: "↕", 19: "‼", 20: "¶", 21: "§", 22: "▬", 23: "↨", 24: "↑", 25: "↓",
    26: "→", 27: "←", 28: "∟", 29: "↔", 30: "▲", 31: "▼", 127: "⌂",
}


def alt_numpad_char(digits):
    """The character Windows composes from Alt + numpad digits, or None.

    Two legacy forms, and the leading zero is what picks between them: "Alt+0152" reads
    the code in the ANSI codepage (cp1252 here) and gives "˜"; "Alt+152" reads it in the
    OEM one (cp850) and gives "ÿ". Values above 255 wrap, as they do in Windows.
    """
    if not digits:
        return None
    n = int(digits) & 0xFF
    # OEM form only: low bytes are box/arrow graphics, not the codepage's control chars
    if not digits.startswith("0") and n in OEM_CONTROL_GRAPHICS:
        return OEM_CONTROL_GRAPHICS[n]
    cp = ctypes.windll.kernel32.GetACP() if digits.startswith("0") \
        else ctypes.windll.kernel32.GetOEMCP()
    try:
        return bytes([n]).decode(f"cp{cp}")
    except (ValueError, LookupError, UnicodeDecodeError):
        return None


def compose_dead(dead, ch):
    """Combine a pending dead key with the next character, as the layout would.

    join() returns the letter plus a loose combining mark when no precomposed character
    exists ("q" + U+0302); Windows types the caret and the letter instead, so only a
    genuine single character counts as a composition.
    """
    try:
        joined = KeyCode.from_dead(dead).join(KeyCode.from_char(ch)).char
    except ValueError:
        joined = None
    return joined if joined and len(joined) == 1 else dead + ch


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
    # system and lock keys
    Key.print_screen: "printscreen", Key.menu: "apps", Key.pause: "pause",
    Key.num_lock: "numlock", Key.scroll_lock: "scrolllock",
    # media keys
    Key.media_volume_up: "volumeup", Key.media_volume_down: "volumedown",
    Key.media_volume_mute: "volumemute", Key.media_play_pause: "playpause",
    Key.media_next: "nexttrack", Key.media_previous: "prevtrack", Key.media_stop: "stop",
}
# F13–F24 (macro keyboards); added only if this pynput build exposes them
SPECIAL.update({k: f"f{n}" for n in range(13, 25)
                if (k := getattr(Key, f"f{n}", None)) is not None})


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
        self._dead = None            # pending dead key ("^" awaiting its vowel)
        self._alt_code = ""          # digits typed so far in an Alt+numpad sequence
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

    def _active_mods(self):
        """ctrl/alt/win/shift currently held — the modifiers that belong on a click or
        scroll (Ctrl+click, Shift+click, Ctrl+scroll to zoom)."""
        return sorted(self._mods) + (["shift"] if self._shift else [])

    def _delay(self):
        now = time.monotonic()
        d = round(now - self._last, 3)
        self._last = now
        return d

    def _flush(self):
        if self._dead:  # "^" then Enter/click: Windows emits the caret on its own
            if not self._text:
                self._text_delay = self._delay()
            self._text += self._dead
            self._dead = None
        if self._text:
            self.steps.append({"t": "text", "text": self._text, "delay": self._text_delay})
            self._text = ""

    def _compose(self, key, ch):
        """Fold a dead-key sequence into the character it produces: "^" then "e" -> "ê".

        Windows composes these inside the keyboard layout, only after the second key.
        A low-level hook sees the two raw presses, and pynput resolves dead keys on X11
        but not on Windows — so without this, typing "você" records as "voc^e".

        Returns the character to record, or None while a dead key is still pending.
        """
        dead, self._dead = self._dead, None
        if dead_diacritic(key, ch) is not None:
            self._dead = ch
            return dead  # pressing two dead keys types the first one literally
        return compose_dead(dead, ch) if dead else ch

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
            self._press = (x, y, button, time.monotonic(), self._delay(),
                           self._active_mods())
            return
        # release: far from the press point -> it was a drag, else a click
        p, self._press = self._press, None
        if p is None:
            return
        x1, y1, btn, t0, delay, mods = p
        if (x - x1) ** 2 + (y - y1) ** 2 >= 100:  # moved 10px or more
            step = {"t": "drag", "x": x1, "y": y1, "x2": x, "y2": y, "button": btn.name,
                    "dur": round(time.monotonic() - t0, 3), "delay": delay}
        else:
            step = {"t": "click", "x": x1, "y": y1, "button": btn.name, "delay": delay}
        if mods:  # Ctrl+click, Shift+click, … — omitted entirely when unmodified
            step["mods"] = mods
        self.steps.append(step)
        self._last = time.monotonic()  # next delay counts from the release

    def _on_scroll(self, x, y, dx, dy):
        if self.paused:
            return
        self._win_candidate = False
        d = self._delay()
        mods = self._active_mods()
        last = self.steps[-1] if self.steps else None
        # coalesce a burst of wheel notches into one step, but only same axis and same
        # modifiers — a Ctrl+scroll zoom must not fold into a plain scroll
        if (not self._text and last and last["t"] == "scroll" and d < 0.3
                and last.get("mods", []) == mods
                and (dx > 0) == (last.get("dx", 0) > 0)
                and (dy > 0) == (last.get("dy", 0) > 0)):
            last["dy"] += dy
            last["dx"] = last.get("dx", 0) + dx
            if not last["dx"]:
                last.pop("dx", None)
            return
        self._flush()
        step = {"t": "scroll", "x": x, "y": y, "dy": dy, "delay": d}
        if dx:  # horizontal wheel / tilt / trackpad — usually zero, so kept optional
            step["dx"] = dx
        if mods:
            step["mods"] = mods
        self.steps.append(step)

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
        # Alt held + numpad digits is Windows' character-code entry ("Alt+0152" -> "˜").
        # Collect the digits; the character only exists once Alt is released, and it is
        # recorded as text so replay types it directly instead of re-entering the code.
        if self._mods == {"alt"} and not self._shift:
            digit = NUMPAD_DIGITS.get(getattr(key, "vk", None))
            if digit is not None:
                self._alt_code += digit
                self._win_candidate = False
                return
        if self._mods:  # ctrl/alt/win held -> hotkey combo
            name = None
            if ch:
                name = chr(ord(ch) + 96) if ord(ch) < 32 else ch  # ctrl yields control chars
            elif key in SPECIAL:
                name = SPECIAL[key]
            if name:
                self._flush()
                self.steps.append({"t": "hotkey", "keys": self._active_mods() + [name],
                                   "delay": self._delay()})
                self._win_candidate = False
            return
        if ch and (ch.isprintable() or ch == " "):
            ch = self._compose(key, ch)
            if ch is None:
                return  # dead key held back until we know what it accents
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
            if MODS[key] == "alt" and self._alt_code:
                # releasing Alt is what makes the character appear
                code, self._alt_code = self._alt_code, ""
                ch = alt_numpad_char(code)
                if ch and not self.paused:
                    if not self._text:
                        self._text_delay = self._delay()
                    else:
                        self._delay()
                    self._text += ch
                return
            if MODS[key] == "win" and self._win_candidate and not self.paused:
                self._win_candidate = False
                self._flush()
                self.steps.append({"t": "key", "key": "win", "delay": self._delay()})
        elif key in SHIFT_KEYS:
            self._shift = False


# ---------------------------------------------------------------- playback

# raw mouse_event flags for the back/forward buttons pyautogui can't drive
_ME = {"x1": (0x0080, 0x0100, 1), "x2": (0x0080, 0x0100, 2)}  # down, up, XBUTTON n


@contextlib.contextmanager
def held_mods(mods):
    """Hold ctrl/alt/win/shift down for the duration of a click or scroll, so a
    recorded Ctrl+click or Ctrl+scroll replays as the modified action, not a bare one."""
    import pyautogui
    for m in mods:
        pyautogui.keyDown(m, _pause=False)
    try:
        yield
    finally:
        for m in reversed(mods):
            pyautogui.keyUp(m, _pause=False)


def mouse_action(x, y, button, down=False, up=False):
    """Click (default), or press/release for a drag. Handles x1/x2 via mouse_event,
    which pyautogui rejects; left/middle/right go through pyautogui as before."""
    import pyautogui
    if button in _ME:
        dn, up_flag, xb = _ME[button]
        pyautogui.moveTo(x, y, _pause=False)
        if not up:
            ctypes.windll.user32.mouse_event(dn, 0, 0, xb, 0)
        if not down:
            ctypes.windll.user32.mouse_event(up_flag, 0, 0, xb, 0)
        return
    if down:
        pyautogui.mouseDown(x, y, button=button, _pause=False)
    elif up:
        pyautogui.mouseUp(x, y, button=button, _pause=False)
    else:
        pyautogui.click(x, y, button=button)


def describe(step):
    t = step["t"]
    mod = "".join(m + "+" for m in step.get("mods", []))  # "ctrl+" prefix, or ""
    if t == "click":
        return f"{mod}{step['button']} click at ({step['x']}, {step['y']})"
    if t == "text":
        txt = step["text"] if len(step["text"]) <= 40 else step["text"][:37] + "..."
        return f'type "{txt}"'
    if t == "key":
        return f"press [{step['key']}]"
    if t == "hotkey":
        return "press [" + " + ".join(step["keys"]) + "]"
    if t == "scroll":
        axis = (f"{'up' if step['dy'] > 0 else 'down'} {abs(step['dy'])}" if step.get("dy")
                else f"{'right' if step.get('dx', 0) > 0 else 'left'} {abs(step.get('dx', 0))}")
        return f"{mod}scroll {axis} at ({step['x']}, {step['y']})"
    if t == "drag":
        return (f"{mod}drag from ({step['x']}, {step['y']}) "
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


def play(steps, on_step=None, cancel=None, pause=None, fixed_delay=None):
    """Replay steps. Returns False if cancelled via the `cancel` callable, else True.

    `pause` is polled like `cancel`; while it reads True playback holds where it is.
    `fixed_delay` replaces every recorded gap — used by flows saved without their timing.
    """
    import pyautogui
    pyautogui.FAILSAFE = True

    # start from screen center so the first glide is consistent regardless of where the
    # cursor happened to be (matches the centered start used when recording)
    sw, sh = pyautogui.size()
    pyautogui.moveTo(sw // 2, sh // 2, _pause=False)

    def cancelled():
        return cancel is not None and cancel()

    def held():
        """Block while paused. False if the user cancelled instead of resuming."""
        while pause is not None and pause():
            if cancelled():
                return False
            time.sleep(0.05)
        return True

    def wait(secs):
        end = time.perf_counter() + secs
        while time.perf_counter() < end:
            if cancelled():
                return False
            if pause is not None and pause():
                t0 = time.perf_counter()
                if not held():
                    return False
                end += time.perf_counter() - t0  # time spent paused doesn't eat the gap
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
        if not held():  # pause also takes hold between zero-delay steps
            return False
        delay = fixed_delay if fixed_delay is not None else s.get("delay", 0)
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
            with held_mods(s.get("mods", [])):  # Ctrl+click, Shift+click, Ctrl+scroll…
                if t == "click":
                    mouse_action(x, y, s["button"])
                elif t == "drag":
                    mouse_action(x, y, s["button"], down=True)
                    time.sleep(0.05)
                    glide(s["x2"], s["y2"], max(0.15, min(s.get("dur", 0.3), 2)))
                    time.sleep(0.05)
                    mouse_action(s["x2"], s["y2"], s["button"], up=True)
                else:
                    # ponytail: 120 = one wheel notch on Windows; make configurable if a mouse/app disagrees
                    if s["dy"]:
                        pyautogui.scroll(int(s["dy"] * 120))
                    if s.get("dx"):
                        pyautogui.hscroll(int(s["dx"] * 120))
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


def wait_before_play(secs, cancel, on_left):
    """Grace period before playback, polled rather than slept so Esc still works.

    The sign-in launcher waits 300s; a plain sleep there left the HUD on screen and
    unresponsive for five minutes, which reads as a freeze. `on_left` is called with
    the whole seconds remaining. Returns False if cancelled during the wait.
    """
    end = time.perf_counter() + max(0, secs)
    while True:
        left = end - time.perf_counter()
        if left <= 0:
            return True
        if cancel():
            return False
        on_left(int(left + 0.999))
        time.sleep(0.1)


def load_recording(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# required keys per step type — the shape play() relies on
_STEP_KEYS = {
    "click": ("x", "y", "button"), "drag": ("x", "y", "x2", "y2", "button"),
    "scroll": ("x", "y"), "text": ("text",), "key": ("key",),
    "hotkey": ("keys",), "path": ("points",),
}


def recording_error(data):
    """A plain-language reason the recording can't be played, or None if it's fine.

    Guards playback against a file that's been hand-edited, truncated, or built by some
    other tool — better a clear message than a mid-run crash driving the mouse."""
    if not isinstance(data, dict):
        return "This file isn't a Pointerizer recording."
    steps = data.get("steps")
    if not isinstance(steps, list):
        return "The recording has no list of steps."
    if not steps:
        return "The recording is empty — there's nothing to play."
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict) or "t" not in s:
            return f"Step {i} is malformed."
        keys = _STEP_KEYS.get(s["t"])
        if keys is None:
            return f"Step {i} is an unknown type ('{s['t']}')."
        missing = [k for k in keys if k not in s]
        if missing:
            return f"Step {i} ({s['t']}) is missing {', '.join(missing)}."
    return None


LOG_PATH = BASE_DIR / "pointerizer-activity.log"


def log_run(name, outcome):
    """Append one line to the activity log so scheduled runs aren't invisible.
    Trims itself so it can't grow without bound."""
    line = f"{datetime.now().isoformat(timespec='seconds')}  {name}  {outcome}\n"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
        if LOG_PATH.stat().st_size > 200_000:  # keep the last ~500 lines
            tail = LOG_PATH.read_text(encoding="utf-8").splitlines()[-500:]
            LOG_PATH.write_text("\n".join(tail) + "\n", encoding="utf-8")
    except OSError:
        pass  # logging must never break a run


def save_recording(name, steps, fixed_delay=None):
    """Write a recording. `fixed_delay` set means replay ignores the recorded gaps and
    uses that constant instead — the original timings stay in the file either way."""
    RECORDINGS_DIR.mkdir(exist_ok=True)
    path = RECORDINGS_DIR / f"{name}.json"
    data = {"name": name, "created": datetime.now().isoformat(timespec="seconds"),
            "steps": steps}
    if fixed_delay is not None:
        data["fixed_delay"] = fixed_delay
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    return path


STARTUP_DIR = (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" /
               "Start Menu" / "Programs" / "Startup")


def startup_path(name):
    return STARTUP_DIR / f"Pointerizer - {name}.cmd"


def startup_script(name):
    """The exact .cmd contents the sign-in launcher for `name` should have today.

    Note it passes --signin, not --wait N: the delay is the app's to decide, so a new
    release can change it without every existing launcher needing to be rewritten.
    """
    target = RECORDINGS_DIR / f"{name}.json"
    if getattr(sys, "frozen", False):
        cmd = f'start "" "{sys.executable}" --play "{target}" --signin'
    else:
        cmd = (f'start "" "{sys.executable}" "{Path(__file__).resolve()}"'
               f' --play "{target}" --signin')
    # UTF-8 + chcp, not mbcs: names may hold characters the system ANSI codepage can't
    # encode (any non-Latin script on a western install), and mbcs raises on those.
    return "@echo off\nchcp 65001 >nul\n" + cmd + "\n"


def set_startup(name, enabled):
    """Create/remove a launcher in the user's Startup folder (runs at Windows sign-in)."""
    p = startup_path(name)
    if not enabled:
        p.unlink(missing_ok=True)
        return
    want = startup_script(name)
    try:
        if p.read_text(encoding="utf-8") == want:
            return  # already current — don't touch the Startup folder needlessly
    except OSError:
        pass
    p.write_text(want, encoding="utf-8")


def refresh_startup_launchers():
    """Bring launchers written by older versions up to the current command line.

    Older builds baked `--wait 300` into the .cmd, and the exe path into it besides, so
    neither a changed delay nor a reinstall to a different folder reached anyone who had
    already ticked the box. Rewriting on launch repairs those in place; set_startup is a
    no-op when the file already matches.
    """
    try:
        stale = list(STARTUP_DIR.glob("Pointerizer - *.cmd"))
    except OSError:
        return
    for f in stale:
        name = f.stem[len("Pointerizer - "):]
        if (RECORDINGS_DIR / f"{name}.json").exists():
            set_startup(name, True)


# ---------------------------------------------------------------- UI

STYLE = """
* { font-family: 'Ubuntu', 'Segoe UI', sans-serif; }
QWidget { background: #212121; color: #ececec; font-size: 13px; }
QLabel { background: transparent; }
QLabel#title { font-size: 24px; font-weight: 500; }
QLabel#subtitle, QLabel#status { color: #9b9b9b; font-size: 12px; }
QListWidget { background: #181818; border: 1px solid #303030; border-radius: 12px;
              padding: 6px; outline: none; }
QWidget#flowrow { background: transparent; }
QLabel#section { color: #cfcfcf; font-size: 12px; font-weight: 500;
                 text-transform: uppercase; letter-spacing: 1px; }
QLabel#rowsub { color: #8a8a8a; font-size: 11px; }
/* transparent background: it inherits QWidget's #212121 and, now that it spans the
   whole row height, would otherwise paint a visible slab behind the tick */
QCheckBox#rowcheck { spacing: 0; background: transparent; }
QCheckBox#rowcheck::indicator { width: 17px; height: 17px; border: 1px solid #4a4a4a;
                                border-radius: 5px; background: transparent; }
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
              border-radius: 10px; padding: 9px 16px; font-weight: 500; }
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
QPushButton#link { background: transparent; border: none; color: #9b9b9b;
                   font-size: 12px; padding: 2px 6px; }
QPushButton#link:hover { color: #ececec; }
QPushButton#link:disabled { color: #565656; }
QPushButton#daychip { background: transparent; border: 1px solid #3a3a3a; color: #9b9b9b;
                      padding: 6px 0; border-radius: 14px; font-size: 12px;
                      font-weight: 500; min-width: 0; }
QPushButton#daychip:hover { background: #2a2a2a; }
QPushButton#daychip:checked { background: #ececec; color: #0d0d0d;
                              border: 1px solid #ececec; }
/* the border matters: with `border: none` Qt paints the background as a plain rect and
   border-radius never clips it, which is what left these buttons square. It matches the
   fill, so it costs nothing visually. */
QPushButton#pillbtn { background: #f5c542; color: #0d0d0d; border: 1px solid #f5c542; }
QPushButton#pillbtn:hover { background: #ffd35c; border-color: #ffd35c; }
QComboBox, QSpinBox, QTimeEdit { background: #181818; border: 1px solid #3a3a3a;
                                 border-radius: 8px; padding: 6px 10px; }
QComboBox:focus, QSpinBox:focus, QTimeEdit:focus { border-color: #6e6e6e; }
QComboBox::drop-down { background: transparent; border: none; width: 24px; }
QSpinBox::up-button, QSpinBox::down-button { background: transparent; border: none;
                                             width: 18px; }
QComboBox QAbstractItemView { background: #181818; border: 1px solid #3a3a3a;
                              selection-background-color: #343434; }
QCheckBox { color: #9b9b9b; spacing: 8px; }
QCheckBox#startuptoggle { padding: 8px 2px; }
QCheckBox:disabled { color: #565656; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #4a4a4a;
                       border-radius: 4px; background: #181818; }
QCheckBox::indicator:checked { background: #dc2626; border-color: #dc2626; }
QFrame#pill { background: #2b2b2b; border: 1px solid #4a4a4a; border-radius: 23px; }
QLabel#recdot { color: #f93a37; font-size: 15px; }
QPushButton#pillbtn { border-radius: 15px; padding: 7px 14px; }
QPushButton#pillfinish { background: #dc2626; color: #ffffff; border: 1px solid #dc2626;
                         border-radius: 15px; padding: 7px 14px; }
QPushButton#pillfinish:hover { background: #ef3b3b; border-color: #ef3b3b; }
QLabel#playdot { font-size: 15px; }
"""


OVERLAY_FLAGS = (QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint |
                 QtCore.Qt.Tool)

PLAY_BORDER = "#8e8ea0"   # shown while replaying, in the GUI and on scheduled runs alike
PAUSE_BORDER = "#f5c542"  # and turns yellow while held at a pause (matches #pillbtn)


class Border(QtWidgets.QWidget):
    """Colored frame around a screen; clicks pass straight through it."""
    def __init__(self, geo, color):
        super().__init__(None, OVERLAY_FLAGS | QtCore.Qt.WindowTransparentForInput |
                         QtCore.Qt.WindowDoesNotAcceptFocus)
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


def fit(widget, *texts):
    """Widest of `texts` in the widget's own (polished) font, in pixels."""
    fm = widget.fontMetrics()
    return max(fm.horizontalAdvance(t) for t in texts)


class PlaybackHud:
    """Screen borders plus a floating pill, so a replay is always visible and its
    controls discoverable. Used by the GUI and by scheduled runs alike.

    Every method must be called from the Qt thread; playback itself runs off-thread.
    """

    def __init__(self, qapp):
        self._borders = [Border(s.geometry(), PLAY_BORDER) for s in qapp.screens()]
        # click-through: the mouse is being driven during playback, so the pill must
        # never swallow a synthetic click that was meant for the app underneath
        self._pill = QtWidgets.QWidget(None, OVERLAY_FLAGS |
                                       QtCore.Qt.WindowTransparentForInput |
                                       QtCore.Qt.WindowDoesNotAcceptFocus)
        self._pill.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self._pill.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
        outer = QtWidgets.QHBoxLayout(self._pill)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QtWidgets.QFrame(objectName="pill")
        outer.addWidget(frame)
        h = QtWidgets.QHBoxLayout(frame)
        h.setContentsMargins(18, 9, 10, 9)
        h.setSpacing(10)
        self._dot = QtWidgets.QLabel("●", objectName="playdot")
        self._status = QtWidgets.QLabel()
        # The recording pill's own button styles, so the two pills cannot drift apart.
        # Never clicked (the window is click-through), hence no focus and no hover.
        self._pause_chip = QtWidgets.QPushButton(objectName="pillbtn")
        cancel_chip = QtWidgets.QPushButton("Cancel (Esc)", objectName="pillfinish")
        for b in (self._pause_chip, cancel_chip):
            b.setFocusPolicy(QtCore.Qt.NoFocus)
            b.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        h.addWidget(self._dot)
        h.addWidget(self._status)
        h.addSpacing(6)
        h.addWidget(self._pause_chip)
        h.addWidget(cancel_chip)
        # Size each caption for its widest text so the pill never jitters as the
        # countdown ticks or the chip flips. ensurePolished() first: before the widget
        # is polished, fontMetrics() reports the default app font rather than the one
        # the stylesheet sets, and the labels come out too narrow and clip.
        self._pill.ensurePolished()
        self._pause_chip.setFixedWidth(fit(self._pause_chip, "Pause (F7)", "Resume (F7)")
                                       + 30)  # + the stylesheet's 14px side padding
        # two widths, swapped per state: sizing the running pill for "Starting in 0:00"
        # would leave a dead gap between "Playing" and the buttons. Within each state
        # the width is constant (M:SS never changes width), so nothing jitters.
        self._w_running = fit(self._status, "Playing", "Paused") + 8
        self._w_waiting = fit(self._status, "Starting in 0:00") + 8
        self._screen = qapp.primaryScreen()
        self._paused = None  # None = never set, so the first sync() always paints

    def show(self, counting_down=False):
        for b in self._borders:
            b.show()
        if counting_down:
            self.countdown(0)
        else:
            self.sync(False)

    def _place(self):
        # captions change width between states; without re-activating the layout the
        # already-shown window keeps its old size and clips the longer text
        self._pill.layout().activate()
        self._pill.resize(self._pill.sizeHint())
        geo = self._screen.geometry()
        self._pill.move(geo.center().x() - self._pill.width() // 2, geo.top() + 24)
        self._pill.show()
        self._pill.raise_()

    def countdown(self, secs):
        """Pre-roll: the HUD is up but nothing is being driven yet. Says so, rather
        than showing 'Playing' over a flow that has not started."""
        if self._paused != "waiting":
            self._paused = "waiting"
            for b in self._borders:
                b.set_color(PLAY_BORDER)
            self._dot.setStyleSheet(f"color: {PLAY_BORDER};")
            self._pause_chip.hide()  # nothing to pause yet; Esc still cancels
            self._status.setFixedWidth(self._w_waiting)
        self._status.setText(f"Starting in {secs // 60}:{secs % 60:02d}")
        self._place()

    def sync(self, paused):
        """Repaint for the current pause state. Cheap to call repeatedly."""
        if paused == self._paused:
            return
        self._paused = paused
        colour = PAUSE_BORDER if paused else PLAY_BORDER
        for b in self._borders:
            b.set_color(colour)
        self._dot.setStyleSheet(f"color: {colour};")
        self._status.setFixedWidth(self._w_running)  # narrower than the countdown state
        self._status.setText("Paused" if paused else "Playing")
        self._pause_chip.setText("Resume (F7)" if paused else "Pause (F7)")
        self._pause_chip.show()
        self._place()

    def close(self):
        self._pill.close()
        for b in self._borders:
            b.close()


ASSET_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "assets"


def apply_theme(qapp):
    """Bundled font + stylesheet. Scheduled runs need it too, or the pill renders
    in Qt's default grey instead of matching the app."""
    for ttf in sorted((ASSET_DIR / "Ubuntu").glob("*.ttf")):
        QtGui.QFontDatabase.addApplicationFont(str(ttf))
    qapp.setStyleSheet(STYLE)


LLKHF_INJECTED = 0x10  # KBDLLHOOKSTRUCT.flags bit marking a synthetic keystroke


def playback_listener(flags):
    """Global hotkeys during playback: Esc cancels, F7 toggles pause.

    F7 toggles on *release* — Windows repeats on_press while a key is held down, which
    would otherwise flip pause on and off many times a second.
    """
    def not_injected(msg, data):
        # The keys we replay come back through this same global hook. Without this
        # filter, replaying a recorded Esc would cancel the playback that just sent it,
        # and a recorded F7 would pause it — flows that close a dialog would abort
        # themselves. Returning False only skips our callbacks; the keystroke still
        # reaches the app being driven.
        # ponytail: also ignores on-screen keyboards and remote-desktop input, which
        # arrive injected too — rare enough to accept, revisit if someone hits it.
        return not (data.flags & LLKHF_INJECTED)

    def on_press(k):
        if k == CANCEL_KEY:
            flags["cancel"] = True

    def on_release(k):
        if k == PAUSE_KEY:
            flags["paused"] = not flags["paused"]

    return keyboard.Listener(on_press=on_press, on_release=on_release,
                             win32_event_filter=not_injected)


def run_ui():
    qapp = QtWidgets.QApplication(sys.argv)
    asset_dir = ASSET_DIR
    apply_theme(qapp)
    icon = asset_dir / "icon.ico"
    if icon.exists():
        qapp.setWindowIcon(QtGui.QIcon(str(icon)))

    icon_font = ("Segoe Fluent Icons" if "Segoe Fluent Icons" in QtGui.QFontDatabase.families()
                 else "Segoe MDL2 Assets")

    ICON_FACTOR = 0.88  # glyph size vs box: these MDL2 glyphs nearly fill the em, so
    #                     leave a small margin to avoid horizontal clipping

    def fluent_icon(glyph, color="#ececec", size=15, box=None):
        # Centre the glyph's INK in the pixmap. drawText(AlignCenter) centres the font's
        # line box instead, and these glyphs sit at different heights within it — every
        # icon then hung below its label, which a hand-tuned downward nudge used to
        # paper over. Qt centres the pixmap against the text, so a centred ink lines up.
        # `box` renders at the button's own size (for icon-only buttons): Qt's centring
        # of a small icon inside a button is a couple of px off, so filling the button
        # and centring here instead puts the glyph dead centre.
        bw, bh = box or (size, size)
        dpr = qapp.devicePixelRatio() or 1
        W, H = max(1, round(bw * dpr)), max(1, round(bh * dpr))
        pm = QtGui.QPixmap(W, H)
        pm.setDevicePixelRatio(dpr)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setRenderHint(QtGui.QPainter.TextAntialiasing)
        f = QtGui.QFont(icon_font)
        f.setPixelSize(max(1, round(size * dpr * ICON_FACTOR)))
        p.setFont(f)
        p.setPen(QtGui.QColor(color))
        # tightBoundingRect is relative to the text origin (its top is above the
        # baseline, hence negative), so subtracting it lands the ink where we want it
        ink = QtGui.QFontMetricsF(f).tightBoundingRect(glyph)
        p.drawText(QtCore.QPointF((W - ink.width()) / 2 - ink.left(),
                                  (H - ink.height()) / 2 - ink.top()), glyph)
        p.end()
        return QtGui.QIcon(pm)

    def set_btn_icon(btn, glyph, color="#ececec", size=15, box=None):
        btn.setIcon(fluent_icon(glyph, color, size, box))
        btn.setIconSize(QtCore.QSize(*(box or (size, size))))

    # Qt's built-in combo/spin arrows are black — invisible on our dark fields.
    # Point the stylesheet at the light chevron PNGs bundled in assets/.
    chev_down = str(asset_dir / "chevron_down.png").replace("\\", "/")
    chev_up = str(asset_dir / "chevron_up.png").replace("\\", "/")
    qapp.setStyleSheet(STYLE + f'''
QComboBox::down-arrow {{ image: url("{chev_down}"); width: 12px; height: 12px; }}
QSpinBox::down-arrow, QTimeEdit::down-arrow {{ image: url("{chev_down}"); width: 10px; height: 10px; }}
QSpinBox::up-arrow, QTimeEdit::up-arrow {{ image: url("{chev_up}"); width: 10px; height: 10px; }}
''')

    GLYPH_CHECK, GLYPH_REDO, GLYPH_STOP, GLYPH_CLOCK, GLYPH_FLAG = (
        chr(0xE73E), chr(0xE72C), chr(0xE71A), chr(0xE823), chr(0xE7C1))
    GLYPH_PLAY, GLYPH_RECORD, GLYPH_TRASH = chr(0xE768), chr(0xE7C8), chr(0xE74D)
    GLYPH_PENCIL = chr(0xE70F)

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
    import_btn = QtWidgets.QPushButton("Import", objectName="link")
    export_btn = QtWidgets.QPushButton("Export", objectName="link")
    for b in (import_btn, export_btn):
        b.setCursor(QtCore.Qt.PointingHandCursor)
        header.addWidget(b)
    del_icon = QtWidgets.QPushButton(objectName="trashbtn")  # shown only when flows selected
    set_btn_icon(del_icon, GLYPH_TRASH, "#ffffff", 15, box=(30, 26))  # centred in the button
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

    startup_cb = QtWidgets.QCheckBox("Run when I sign in to Windows", objectName="startuptoggle")
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

    state = {"recorder": None, "name": "", "play_result": None, "pill_rect": None,
             "hud": None, "play_flags": None, "pre_roll": None}
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
        if rec and not rec.paused:  # while paused at a checkpoint, Continue resets the
            #                         clock itself — resuming here would un-pause it
            # Moving the pill is not part of the flow, so it interrupts nothing: the
            # click itself is already dropped by in_own_ui, and resetting the clock
            # here discards the time spent dragging. Recording carries straight on
            # from the last real action — use F8 when you actually want a checkpoint.
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
        # ticking a box also makes that flow the current row, so the sign-in toggle
        # (bound via currentItemChanged) applies to it without a second click on the row
        select_by_name(name)
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
        # span the full name+schedule block instead of floating beside it; the 17px
        # indicator still centres itself, so the tick lines up with the two-line text
        cb.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.MinimumExpanding)
        h.addWidget(cb)

        texts = QtWidgets.QVBoxLayout()
        texts.setSpacing(4)  # breathing room between the flow name and its schedule line
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
            set_btn_icon(b, glyph, "#9b9b9b", 13)
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
        v.setContentsMargins(20, 18, 20, 18)  # match the rename/schedule dialogs
        v.setSpacing(12)
        new = rec.steps[rec.checkpoint:]
        head = QtWidgets.QLabel(f"{len(new)} action(s) since last checkpoint")
        head.setStyleSheet("font-size: 15px; font-weight: 500;")
        v.addWidget(head)
        box = QtWidgets.QListWidget()
        for s in new:
            box.addItem(describe(s))
        v.addWidget(box, stretch=1)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
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
            b.setFixedHeight(40)          # equal-width, equal-height row like the main buttons
            row.addWidget(b, stretch=1)
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
        head.setStyleSheet("font-size: 15px; font-weight: 500;")
        v.addWidget(head)
        edit = QtWidgets.QLineEdit(default)
        edit.selectAll()  # Enter keeps the default; typing replaces it
        v.addWidget(edit)
        keep = QtWidgets.QCheckBox("Keep my original timing")
        keep.setChecked(True)
        v.addWidget(keep)
        hint = QtWidgets.QLabel(f"Unticked, every step replays {FIXED_DELAY:g}s apart — "
                                "faster, and it drops the pauses you took while recording.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9a9a9a; font-size: 12px;")
        v.addWidget(hint)
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
        name = re.sub(r"[^\w\- ]", "", edit.text().strip()) or default
        return name, (None if keep.isChecked() else FIXED_DELAY)

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
        # availableGeometry, not geometry: it already excludes the taskbar, so the pill
        # clears it whatever its height or edge, and the margin is a real gap
        scr = win.screen().availableGeometry()
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
            chosen = prompt_name(default)
            if chosen:
                name, fixed = chosen
                name = unique_name(name)
                save_recording(name, rec.steps, fixed)
                timing = "original timing" if fixed is None else f"{fixed:g}s steps"
                status.setText(f"Saved '{name}' ({len(rec.steps)} steps, {timing})")
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
        try:
            data = load_recording(path)
        except (OSError, json.JSONDecodeError):
            status.setText("Couldn't read that recording — the file looks damaged.")
            return
        problem = recording_error(data)
        if problem:
            status.setText(problem)
            return
        set_busy(True)
        win.hide()
        state["hud"] = PlaybackHud(qapp)
        state["hud"].show(counting_down=True)
        flags = state["play_flags"] = {"cancel": False, "paused": False}
        pre = state["pre_roll"] = {"left": 2, "running": False}

        def worker():
            lst = playback_listener(flags)
            lst.start()
            try:
                # 2s to bring the target window to front — Esc works during it too
                if not wait_before_play(2, lambda: flags["cancel"],
                                        lambda n: pre.__setitem__("left", n)):
                    state["play_result"] = "Playback cancelled (Esc)"
                    return
                pre["running"] = True
                if play(data["steps"], cancel=lambda: flags["cancel"],
                        pause=lambda: flags["paused"],
                        fixed_delay=data.get("fixed_delay")):
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
        hud, flags, pre = state.get("hud"), state.get("play_flags"), state.get("pre_roll")
        if hud and flags and pre:  # repainted on the Qt thread; playback runs off it
            hud.sync(flags["paused"]) if pre["running"] else hud.countdown(pre["left"])
        if state["play_result"] is None:
            return
        play_poll.stop()
        if hud:
            hud.close()
            state["hud"] = None
        win.show()
        win.raise_()
        win.activateWindow()
        set_busy(False)
        status.setText(state["play_result"])
        log_run(selected_name() or "?", state["play_result"])

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

    def do_import():
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            win, "Import recordings", "", "Pointerizer recordings (*.json)")
        added = 0
        for f in files:
            try:
                data = load_recording(Path(f))
            except (OSError, json.JSONDecodeError):
                data = None
            if data is None or recording_error(data):
                status.setText(f"'{Path(f).stem}' isn't a valid recording — skipped.")
                continue
            base = re.sub(r"[^\w\- ]", "", str(data.get("name") or Path(f).stem)) or "Imported"
            name = unique_name(base)
            data["name"] = name
            data.pop("schedule", None)  # a schedule belongs to the machine, not the file
            with open(RECORDINGS_DIR / f"{name}.json", "w", encoding="utf-8") as out:
                json.dump(data, out, indent=1)
            added += 1
        if added:
            status.setText(f"Imported {added} recording(s).")
            refresh()

    def do_export():
        name = selected_name()
        if not name:
            status.setText("Select a flow to export first.")
            return
        dest, _ = QtWidgets.QFileDialog.getSaveFileName(
            win, "Export recording", f"{name}.json", "Pointerizer recording (*.json)")
        if not dest:
            return
        try:
            Path(dest).write_bytes((RECORDINGS_DIR / f"{name}.json").read_bytes())
            status.setText(f"Exported '{name}'.")
        except OSError as e:
            status.setText(f"Couldn't export: {e}")

    def rename_flow(name):
        src = RECORDINGS_DIR / f"{name}.json"
        try:
            data = load_recording(src)
        except Exception:
            status.setText(f"Can't read '{name}'.")
            return
        dlg = QtWidgets.QDialog(win)
        dlg.setWindowTitle("Edit recording")
        dlg.setFixedWidth(420)
        v = QtWidgets.QVBoxLayout(dlg)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(12)
        head = QtWidgets.QLabel("Name")
        head.setStyleSheet("font-size: 15px; font-weight: 500;")
        v.addWidget(head)
        edit = QtWidgets.QLineEdit(name)
        edit.selectAll()
        v.addWidget(edit)
        keep = QtWidgets.QCheckBox("Keep my original timing")
        keep.setChecked("fixed_delay" not in data)  # reflects how it's saved today
        v.addWidget(keep)
        hint = QtWidgets.QLabel(f"Unticked, every step replays {FIXED_DELAY:g}s apart — "
                                "faster, and it drops the pauses you took while recording.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9a9a9a; font-size: 12px;")
        v.addWidget(hint)
        row = QtWidgets.QHBoxLayout()
        okb = QtWidgets.QPushButton("Save", objectName="primary")
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
        # timing is editable on its own — applied whether or not the name changed
        if keep.isChecked():
            data.pop("fixed_delay", None)
        else:
            data["fixed_delay"] = FIXED_DELAY
        timing = "original timing" if keep.isChecked() else f"{FIXED_DELAY:g}s steps"

        new = re.sub(r"[^\w\- ]", "", edit.text().strip())
        if not new or new == name:  # timing-only edit: rewrite in place, keep schedules
            with open(src, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            status.setText(f"Updated '{name}' ({timing})")
            refresh()
            select_by_name(name)
            return
        new_p = RECORDINGS_DIR / f"{new}.json"
        if new_p.exists():
            status.setText(f"'{new}' already exists.")
            return
        data["name"] = new  # already carries the timing choice made above
        data.pop("schedule", None)  # its Task Scheduler task is dropped below
        with open(new_p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        src.unlink()
        if startup_path(name).exists():  # migrate the sign-in launcher
            set_startup(name, False)
            set_startup(new, True)
        # a scheduled task points at the old file; drop it rather than fire a broken one
        r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", f"Pointerizer - {name}"],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode == 0:
            status.setText(f"Renamed to '{new}' ({timing}) — its schedule was removed, "
                           "re-create it.")
        else:
            status.setText(f"Renamed to '{new}' ({timing})")
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
        head.setStyleSheet("font-size: 15px; font-weight: 500;")
        v.addWidget(head)
        form = QtWidgets.QFormLayout()
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        freq = QtWidgets.QComboBox()
        freq.addItems(["Daily", "Hourly", "Weekly"])
        now = QtCore.QTime.currentTime().addSecs(600)
        # QTimeEdit, not two dropdowns: type any HH:MM to the minute (or spin/arrow it),
        # instead of being pinned to 5-minute steps
        time_edit = QtWidgets.QTimeEdit(now)
        time_edit.setDisplayFormat("HH:mm")
        time_edit.setFixedWidth(90)
        trow = QtWidgets.QHBoxLayout()
        trow.addWidget(time_edit)
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
            st = time_edit.time().toString("HH:mm")
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
    import_btn.clicked.connect(do_import)
    export_btn.clicked.connect(do_export)
    export_btn.setEnabled(False)
    listw.currentItemChanged.connect(lambda *_: export_btn.setEnabled(bool(selected_name())))
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

    def maybe_welcome():
        settings = QtCore.QSettings("Pointerizer", "Pointerizer")
        if settings.value("welcomed", False, type=bool):
            return
        m = QtWidgets.QMessageBox(win)
        m.setWindowTitle("Welcome to Pointerizer")
        m.setTextFormat(QtCore.Qt.RichText)
        m.setText("<b>Record what you do, then replay it whenever you like.</b>")
        m.setInformativeText(
            "• <b>Record (F9)</b> — do your task, then press <b>F9</b> again to stop "
            "and name it.<br>"
            "• <b>F8</b> pauses to review as you go; <b>Esc</b> (or shoving the mouse "
            "into a screen corner) cancels.<br>"
            "• <b>Play</b> a saved flow, or schedule it to run on its own.<br><br>"
            "Your recordings are plain files you can export and share. One tip: avoid "
            "recording passwords, since they're saved as readable text.")
        m.setStandardButtons(QtWidgets.QMessageBox.Ok)
        m.button(QtWidgets.QMessageBox.Ok).setText("Got it")
        m.exec()
        settings.setValue("welcomed", True)  # shown once; Esc counts as seen

    refresh_startup_launchers()  # repair launchers left by an older build
    refresh()
    win.show()
    maybe_welcome()
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
    # recording validation: good file passes, common breakages give a reason
    assert recording_error(loaded) is None
    assert recording_error({"steps": []})                       # empty
    assert recording_error({"steps": [{"t": "click", "x": 1}]}) # click missing y/button
    assert recording_error({"steps": [{"t": "wat"}]})           # unknown type
    assert recording_error("not a dict")
    assert "fixed_delay" not in loaded  # keeping original timing writes no override
    fixed = load_recording(save_recording("_selfcheck", steps, FIXED_DELAY))
    assert fixed["fixed_delay"] == FIXED_DELAY
    assert fixed["steps"] == steps      # the recorded gaps survive the override
    # the edit dialog toggles that key both ways; re-ticking must leave no residue
    fixed.pop("fixed_delay", None)
    assert "fixed_delay" not in fixed and fixed["steps"] == steps
    # the pre-play wait must be interruptible — a plain sleep here made the 300s
    # sign-in grace period look like a frozen app with a dead Esc key
    seen = []
    t0 = time.perf_counter()
    assert wait_before_play(300, lambda: len(seen) >= 3, seen.append) is False
    assert time.perf_counter() - t0 < 5, "cancel did not interrupt the wait"
    assert seen and seen[0] == 300  # counts down from the full wait
    assert wait_before_play(0, lambda: False, seen.append) is True
    # the sign-in launcher must not freeze the delay into its text, or changing
    # SIGNIN_WAIT in a later release would never reach anyone already set up
    script = startup_script("_selfcheck")
    assert "--signin" in script and "--wait" not in script
    assert str(SIGNIN_WAIT) not in script
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
    # dead keys: "^" then "e" is one character, "ê" — not the two Windows delivers
    assert compose_dead("^", "e") == "ê"
    assert compose_dead("~", "a") == "ã"
    assert compose_dead("´", "o") == "ó"
    assert compose_dead("^", " ") == "^"   # dead key then space types the mark alone
    assert compose_dead("^", "q") == "^q"  # no precomposed "q̂": Windows types both
    r = Recorder()
    r._dead = "^"                          # abandoned dead key still reaches the text
    r._flush()
    assert r.steps[-1]["text"] == "^" and r._dead is None
    r = Recorder()                         # a plain letter is untouched
    assert r._compose(KeyCode.from_char("e"), "e") == "e" and r._dead is None

    # Alt + numpad character codes: the digits are swallowed and the composed
    # character is recorded as text, so replay types it instead of re-entering the code
    assert alt_numpad_char("0152") == "˜"   # leading zero -> ANSI codepage
    assert alt_numpad_char("0233") == "é"
    assert alt_numpad_char("26") == "→"     # OEM low byte -> CP437 graphic, not U+001A
    assert alt_numpad_char("1") == "☺"
    assert alt_numpad_char("2") == "☻"
    assert alt_numpad_char("65") == "A"     # OEM printable range is unchanged
    assert alt_numpad_char("0009") == "\t"  # leading-zero control stays a control char
    assert alt_numpad_char("") is None
    r = Recorder()
    r._on_press(Key.alt)
    for vk in (0x60, 0x61, 0x65, 0x62):          # numpad 0, 1, 5, 2
        r._on_press(KeyCode.from_vk(vk))
    assert r._alt_code == "0152" and not r.steps  # nothing emitted mid-sequence
    r._on_release(Key.alt)
    r._flush()
    assert r.steps[-1]["t"] == "text" and r.steps[-1]["text"] == "˜"
    r = Recorder()                                # Alt+Tab must stay a plain hotkey
    r._on_press(Key.alt); r._on_press(Key.tab); r._on_release(Key.alt)
    assert [s["t"] for s in r.steps] == ["hotkey"] and r.steps[0]["keys"] == ["alt", "tab"]

    # modified clicks/scroll: the held modifier must ride along, or Ctrl+click
    # (multi-select) and Ctrl+scroll (zoom) replay as bare actions
    from pynput.mouse import Button
    r = Recorder()
    r._on_press(Key.ctrl)
    r._on_click(9, 9, Button.left, True); r._on_click(9, 9, Button.left, False)
    r._on_release(Key.ctrl)
    assert r.steps[-1]["t"] == "click" and r.steps[-1]["mods"] == ["ctrl"]
    r = Recorder()                                # plain click carries no mods key
    r._on_click(9, 9, Button.left, True); r._on_click(9, 9, Button.left, False)
    assert "mods" not in r.steps[-1]
    r = Recorder()                                # Ctrl+scroll stays separate from plain
    r._on_scroll(1, 1, 0, -1)
    r._on_press(Key.ctrl); r._on_scroll(1, 1, 0, -1); r._on_release(Key.ctrl)
    assert len(r.steps) == 2 and r.steps[1]["mods"] == ["ctrl"]
    r = Recorder()                                # horizontal wheel keeps dx
    r._on_scroll(1, 1, -2, 0)
    assert r.steps[-1]["dx"] == -2
    assert "left" in describe({"t": "scroll", "x": 0, "y": 0, "dx": -2, "dy": 0})
    assert describe({"t": "click", "x": 0, "y": 0, "button": "left",
                     "mods": ["ctrl"]}).startswith("ctrl+")

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
    ap.add_argument("--signin", action="store_true",
                    help=f"sign-in run: wait {SIGNIN_WAIT}s, whatever this build's "
                         "startup delay is, instead of --wait")
    ap.add_argument("--selfcheck", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.selfcheck:
        selfcheck()
    elif args.play:
        path = Path(args.play)
        if not path.exists():
            path = RECORDINGS_DIR / f"{args.play}.json"
        try:
            data = load_recording(path)
            problem = recording_error(data)
        except (OSError, json.JSONDecodeError) as e:
            data, problem = None, f"could not read recording: {e}"
        if problem:
            print(problem, file=sys.stderr)
            log_run(path.stem, problem)
            sys.exit(1)
        flags = {"cancel": False, "paused": False}
        playback_listener(flags).start()

        # Same border and pill the GUI shows while replaying, so a scheduled run is
        # never a silent takeover of the mouse and its controls stay discoverable.
        # Playback runs off-thread; Qt owns the main thread so the overlays paint.
        qapp = QtWidgets.QApplication(sys.argv)
        apply_theme(qapp)
        hud = PlaybackHud(qapp)
        wait = SIGNIN_WAIT if args.signin else args.wait
        hud.show(counting_down=wait > 0)
        pre = {"left": max(0, wait), "running": False}

        def tick():  # HUD repaints on the Qt thread; playback runs off it
            if pre["running"]:
                hud.sync(flags["paused"])
            else:
                hud.countdown(pre["left"])

        QtCore.QTimer(qapp, interval=200, timeout=tick).start()

        failed = []  # non-empty => exit non-zero so Task Scheduler records the failure

        def worker():
            # try/finally is load-bearing: the corner-slam failsafe raises out of
            # play(), and without the quit the process would sit there forever with
            # the border stuck on screen — as a scheduled task, invisibly, every run.
            try:
                # grace period (desktop settle time) — cancellable, and counted down
                # on the pill so a long --wait doesn't look like a hang
                if not wait_before_play(wait, lambda: flags["cancel"],
                                        lambda n: pre.__setitem__("left", n)):
                    return
                pre["running"] = True
                for _ in range(max(1, args.repeat)):
                    if not play(data["steps"], cancel=lambda: flags["cancel"],
                                pause=lambda: flags["paused"],
                                fixed_delay=data.get("fixed_delay")):
                        break
            except Exception as e:
                print(f"playback aborted: {e}", file=sys.stderr)
                failed.append(e)
            finally:
                QtCore.QMetaObject.invokeMethod(qapp, "quit", QtCore.Qt.QueuedConnection)

        threading.Thread(target=worker, daemon=True).start()
        qapp.exec()
        log_run(data["name"] if isinstance(data.get("name"), str) else path.stem,
                f"aborted: {failed[0]}" if failed else "played")
        if failed:
            sys.exit(1)
    else:
        run_ui()


if __name__ == "__main__":
    main()
