"""
eqchat - post a line into EverQuest's chat via the Windows clipboard + Ctrl+V.

Used by eqdps_ui to auto-post a parse to group chat when you fire an in-game
trigger. Pasting (rather than synthesizing each keypress) is far more reliable
inside EQ's old DirectX client. The text to send must already be on the clipboard;
this module only finds/focuses the EQ window and drives Enter / Ctrl+V / Enter.

NOTE: this is input automation. Some emulated-server rules forbid it. Off by default
in the UI unless you enable the toggle.
"""

import time
import ctypes
import ctypes.wintypes as wt

user32 = ctypes.windll.user32

VK_RETURN, VK_CONTROL, VK_V = 0x0D, 0x11, 0x56
KEYEVENTF_KEYUP = 0x0002


def find_eq_hwnd():
    """Return the EverQuest window handle, or 0 if not found."""
    h = user32.FindWindowW("EverQuest", None)   # EQ's registered window class
    if h:
        return h
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if "EverQuest" in buf.value:
                found.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else 0


def _tap(vk, hold=0.03):
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(hold)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def _paste():
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    time.sleep(0.04)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def post(hwnd=None):
    """With the message already on the clipboard, open EQ chat, paste, and send.
    Returns True if the EQ window was found and driven, else False."""
    hwnd = hwnd or find_eq_hwnd()
    if not hwnd:
        return False
    try:
        user32.SetForegroundWindow(hwnd)   # best-effort; EQ is usually already foreground
    except Exception:
        pass
    time.sleep(0.10)
    _tap(VK_RETURN)     # open the chat input line
    time.sleep(0.12)
    _paste()            # paste "/g <parse>"
    time.sleep(0.12)
    _tap(VK_RETURN)     # send
    return True
