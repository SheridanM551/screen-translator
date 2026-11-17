"""System-wide hotkeys via Win32 RegisterHotKey on a dedicated message-loop thread.

    hk = GlobalHotkeys()
    hk.register("ctrl+alt+t", on_toggle)      # raises HotkeyError if taken / invalid
    ...
    hk.stop()

Callbacks run on the hotkey thread; marshal to the GUI thread yourself (e.g. emit a Qt
signal). MOD_NOREPEAT is always set, so holding the keys fires once.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import queue
import threading
import traceback
from typing import Callable

__all__ = ["GlobalHotkeys", "HotkeyError", "parse_hotkey"]

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_QUIT, WM_HOTKEY, WM_USER, WM_APP = 0x0012, 0x0312, 0x0400, 0x8000
_WM_CMD = WM_APP + 0x1F
PM_NOREMOVE = 0
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "ctl": MOD_CONTROL,
         "alt": MOD_ALT, "menu": MOD_ALT,
         "shift": MOD_SHIFT,
         "win": MOD_WIN, "super": MOD_WIN, "meta": MOD_WIN, "cmd": MOD_WIN, "windows": MOD_WIN}

_KEYS: dict[str, int] = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "back": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21, "prior": 0x21,
    "pagedown": 0x22, "pgdn": 0x22, "next": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "prtsc": 0x2C, "print": 0x2C, "snapshot": 0x2C,
    "pause": 0x13, "break": 0x13, "capslock": 0x14, "scrolllock": 0x91, "numlock": 0x90,
    "apps": 0x5D, "menukey": 0x5D, "contextmenu": 0x5D,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    "plus": 0xBB, "=": 0xBB, "equal": 0xBB, "equals": 0xBB,
    "minus": 0xBD, "-": 0xBD, "comma": 0xBC, ",": 0xBC, "period": 0xBE, ".": 0xBE,
    "semicolon": 0xBA, ";": 0xBA, "slash": 0xBF, "/": 0xBF, "grave": 0xC0, "backquote": 0xC0,
    "`": 0xC0, "tilde": 0xC0, "[": 0xDB, "lbracket": 0xDB, "]": 0xDD, "rbracket": 0xDD,
    "\\": 0xDC, "backslash": 0xDC, "'": 0xDE, "quote": 0xDE, "apostrophe": 0xDE,
    "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD,
    "medianext": 0xB0, "mediaprev": 0xB1, "mediastop": 0xB2, "mediaplaypause": 0xB3,
}
_KEYS.update({chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)})   # 'a' -> 0x41
_KEYS.update({str(d): 0x30 + d for d in range(10)})
_KEYS.update({f"f{i}": 0x6F + i for i in range(1, 25)})                  # F1 = 0x70
_KEYS.update({f"num{d}": 0x60 + d for d in range(10)})
_KEYS.update({f"numpad{d}": 0x60 + d for d in range(10)})


class HotkeyError(RuntimeError):
    pass


def parse_hotkey(combo: str) -> tuple[int, int]:
    """'ctrl+alt+t' -> (modifiers, virtual-key). Names are case-insensitive; '+' as the
    key itself can be written 'plus' (or 'ctrl++')."""
    s = combo.strip().lower().replace(" ", "")
    if not s:
        raise HotkeyError("empty hotkey")
    parts = s.split("+")
    if s.endswith("++"):                       # e.g. 'ctrl++'
        parts = s[:-2].split("+") + ["plus"]
    parts = [p for p in parts if p]
    *mods, key = parts
    mod = 0
    for m in mods:
        if m not in _MODS:
            raise HotkeyError(f"unknown modifier {m!r} in {combo!r} (use ctrl/alt/shift/win)")
        mod |= _MODS[m]
    if key in _MODS:
        raise HotkeyError(f"{combo!r} has no non-modifier key")
    if key not in _KEYS:
        raise HotkeyError(f"unknown key {key!r} in {combo!r}")
    return mod, _KEYS[key]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
_user32.RegisterHotKey.restype = wt.BOOL
_user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wt.BOOL
_user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
_user32.GetMessageW.restype = wt.BOOL
_user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
_user32.PeekMessageW.restype = wt.BOOL
_user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.PostThreadMessageW.restype = wt.BOOL
_kernel32.GetCurrentThreadId.restype = wt.DWORD


class GlobalHotkeys:
    """RegisterHotKey wrapper. Thread-safe; callbacks run on the internal hotkey thread."""

    def __init__(self) -> None:
        self._cbs: dict[int, tuple[str, Callable[[], None]]] = {}
        self._ids: dict[tuple[int, int], int] = {}
        self._next_id = 1
        self._cmds: "queue.Queue[tuple]" = queue.Queue()
        self._tid = 0
        self._ready = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="GlobalHotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        if not self._tid:
            raise HotkeyError("hotkey thread failed to start")

    # ---- public -------------------------------------------------------------------
    def register(self, combo: str, callback: Callable[[], None]) -> int:
        """Register a system-wide hotkey. Returns its id. Raises HotkeyError if the
        combination is invalid or already taken (by another program or by us)."""
        mod, vk = parse_hotkey(combo)
        return self._call("reg", combo, mod, vk, callback)

    def unregister(self, combo_or_id: str | int) -> None:
        if isinstance(combo_or_id, str):
            mod, vk = parse_hotkey(combo_or_id)
            hid = self._ids.get((mod, vk))
            if hid is None:
                return
        else:
            hid = combo_or_id
        self._call("unreg", hid)

    def registered(self) -> list[str]:
        return [c for c, _ in self._cbs.values()]

    def stop(self) -> None:
        """Unregister everything and end the message loop (idempotent)."""
        if not self._thread.is_alive():
            return
        try:
            self._call("stop", timeout=2.0)
        except HotkeyError:
            pass
        _user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        self._thread.join(2.0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass

    # ---- plumbing -----------------------------------------------------------------
    def _call(self, *cmd, timeout: float = 5.0):
        if threading.get_ident() == self._thread.ident:      # called from a callback
            return self._exec(cmd)
        if not self._thread.is_alive():
            raise HotkeyError("hotkey thread is not running (stop() was called)")
        done = threading.Event()
        box: list = [None, None]
        self._cmds.put((cmd, done, box))
        if not _user32.PostThreadMessageW(self._tid, _WM_CMD, 0, 0):
            raise HotkeyError(f"PostThreadMessageW failed ({ctypes.get_last_error()})")
        if not done.wait(timeout):
            raise HotkeyError("hotkey thread did not respond")
        if box[1] is not None:
            raise box[1]
        return box[0]

    def _exec(self, cmd):
        op = cmd[0]
        if op == "reg":
            _, combo, mod, vk, cb = cmd
            if (mod, vk) in self._ids:
                raise HotkeyError(f"hotkey {combo!r} is already registered by this program")
            hid = self._next_id
            if not _user32.RegisterHotKey(None, hid, mod | MOD_NOREPEAT, vk):
                err = ctypes.get_last_error()
                if err == ERROR_HOTKEY_ALREADY_REGISTERED:
                    raise HotkeyError(f"hotkey {combo!r} is already in use by another program "
                                      "(or Windows); pick a different combination")
                raise HotkeyError(f"RegisterHotKey({combo!r}) failed: {ctypes.FormatError(err)} ({err})")
            self._next_id += 1
            self._cbs[hid] = (combo, cb)
            self._ids[(mod, vk)] = hid
            return hid
        if op == "unreg":
            hid = cmd[1]
            if hid in self._cbs:
                _user32.UnregisterHotKey(None, hid)
                del self._cbs[hid]
                self._ids = {k: v for k, v in self._ids.items() if v != hid}
            return None
        if op == "stop":
            for hid in list(self._cbs):
                _user32.UnregisterHotKey(None, hid)
            self._cbs.clear()
            self._ids.clear()
            return None
        raise HotkeyError(f"bad command {op!r}")

    def _drain(self) -> None:
        while True:
            try:
                cmd, done, box = self._cmds.get_nowait()
            except queue.Empty:
                return
            try:
                if self._closed:
                    raise HotkeyError("hotkey thread is stopping")
                box[0] = self._exec(cmd)
            except BaseException as e:  # noqa: BLE001 - handed back to the caller
                box[1] = e
            done.set()

    def _run(self) -> None:
        msg = wt.MSG()
        self._tid = _kernel32.GetCurrentThreadId()
        _user32.PeekMessageW(ctypes.byref(msg), None, WM_USER, WM_USER, PM_NOREMOVE)  # make queue
        self._ready.set()
        try:
            while True:
                r = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r == 0 or r == -1:
                    break
                if msg.message == WM_HOTKEY:
                    ent = self._cbs.get(int(msg.wParam))
                    if ent:
                        try:
                            ent[1]()
                        except Exception:  # keep the loop alive
                            traceback.print_exc()
                elif msg.message == _WM_CMD:
                    self._drain()
        finally:
            for hid in list(self._cbs):
                _user32.UnregisterHotKey(None, hid)
            self._cbs.clear()
            self._ids.clear()
            self._closed = True
            self._drain()   # fail any pending callers instead of leaving them waiting
