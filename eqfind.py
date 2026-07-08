"""
eqfind - locate the EverQuest 'Logs' folder automatically, and remember it.

Resolution order (first hit wins):
    1. an explicit override (e.g. --logs on the command line)
    2. a previously saved choice (%LOCALAPPDATA%\\eqdps\\config.json)
    3. auto-detection:
         a. a running eqgame.exe  -> its folder\\Logs   (most reliable while playing)
         b. the Daybreak launcher install location (Public / ProgramData)
         c. Steam libraries (parsed from libraryfolders.vdf)
         d. legacy Sony / common install roots
Anything auto-detected is saved so the next launch is instant.

No third-party packages - stdlib only.
"""

import os
import re
import glob
import json

CONFIG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "slopmeter")
CONFIG = os.path.join(CONFIG_DIR, "config.json")


# ---------------- config persistence ----------------
def load():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_logs_dir(path):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        data = load()
        data["logs_dir"] = path
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


# ---------------- validation / normalization ----------------
def _has_eqlogs(d):
    return bool(glob.glob(os.path.join(d, "eqlog_*.txt")))


def is_logs_dir(d):
    """A plausible EQ Logs folder: named Logs, or holds eqlog_*.txt, or sits next
    to eqclient.ini / eqgame.exe."""
    if not d or not os.path.isdir(d):
        return False
    if _has_eqlogs(d):
        return True
    parent = os.path.dirname(d.rstrip("\\/"))
    return (os.path.basename(d.rstrip("\\/")).lower() == "logs"
            and (os.path.exists(os.path.join(parent, "eqclient.ini"))
                 or os.path.exists(os.path.join(parent, "eqgame.exe"))))


def normalize(path):
    """Coerce whatever the user pointed at (install root OR Logs folder) into a
    Logs folder. Returns the Logs path, or None if it doesn't look like EQ."""
    if not path:
        return None
    path = os.path.normpath(path)
    if os.path.isfile(path):
        path = os.path.dirname(path)
    # they pointed at an install root
    if os.path.exists(os.path.join(path, "eqgame.exe")) or os.path.exists(os.path.join(path, "eqclient.ini")):
        logs = os.path.join(path, "Logs")
        if os.path.isdir(logs):
            return logs
        os.makedirs(logs, exist_ok=True)   # exists once /log on runs
        return logs
    if is_logs_dir(path):
        return path
    # maybe they picked the parent of Logs
    logs = os.path.join(path, "Logs")
    if is_logs_dir(logs):
        return logs
    return None


# ---------------- detection sources ----------------
def _running_eq_dir():
    """Folder of a running eqgame.exe, via the Toolhelp snapshot API (ctypes)."""
    try:
        import ctypes
        from ctypes import wintypes as w

        k = ctypes.windll.kernel32
        k.CreateToolhelp32Snapshot.restype = w.HANDLE
        k.OpenProcess.restype = w.HANDLE
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.CloseHandle.argtypes = [w.HANDLE]

        class PE32(ctypes.Structure):
            _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD),
                        ("th32ProcessID", w.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                        ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", w.DWORD), ("szExeFile", ctypes.c_char * 260)]

        snap = k.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
        if not snap or snap == w.HANDLE(-1).value:
            return None
        e = PE32(); e.dwSize = ctypes.sizeof(e)
        pid = None
        k.Process32First.argtypes = [w.HANDLE, ctypes.POINTER(PE32)]
        k.Process32Next.argtypes = [w.HANDLE, ctypes.POINTER(PE32)]
        if k.Process32First(snap, ctypes.byref(e)):
            while True:
                if e.szExeFile.lower() == b"eqgame.exe":
                    pid = e.th32ProcessID
                    break
                if not k.Process32Next(snap, ctypes.byref(e)):
                    break
        k.CloseHandle(snap)
        if not pid:
            return None
        h = k.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        buf = ctypes.create_unicode_buffer(32768)
        size = w.DWORD(len(buf))
        ok = k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        k.CloseHandle(h)
        return os.path.dirname(buf.value) if ok else None
    except Exception:
        return None


def _steam_libraries():
    """Steam library roots parsed from libraryfolders.vdf."""
    roots = []
    for base in (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("ProgramFiles", r"C:\Program Files")):
        vdf = os.path.join(base, "Steam", "steamapps", "libraryfolders.vdf")
        if os.path.exists(vdf):
            try:
                text = open(vdf, encoding="utf-8", errors="ignore").read()
                roots += re.findall(r'"path"\s+"([^"]+)"', text)
            except OSError:
                pass
        roots.append(os.path.join(base, "Steam"))
    return roots


def _candidates():
    pub = os.environ.get("PUBLIC", r"C:\Users\Public")
    pdata = os.environ.get("ProgramData", r"C:\ProgramData")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

    globs = [
        os.path.join(pub, "Daybreak Game Company", "Installed Games", "*", "Logs"),
        os.path.join(pdata, "Daybreak Game Company", "Installed Games", "*", "Logs"),
        os.path.join(pf86, "Sony", "*", "Logs"),
        os.path.join(pf, "Sony", "*", "Logs"),
        r"C:\EverQuest*\Logs", r"C:\Games\*EverQuest*\Logs", r"C:\*EverQuest*\Logs",
    ]
    for lib in _steam_libraries():
        globs.append(os.path.join(lib.replace("\\\\", "\\"), "steamapps", "common", "*verQuest*", "Logs"))

    running = _running_eq_dir()
    found = []
    if running:
        found.append(os.path.join(running, "Logs"))
    for g in globs:
        found += glob.glob(g)
    # de-dupe, keep only real EQ Logs dirs
    seen, out = set(), []
    for d in found:
        d = os.path.normpath(d)
        key = d.lower()
        if key in seen:
            continue
        seen.add(key)
        if is_logs_dir(d) or (os.path.basename(d).lower() == "logs" and os.path.isdir(d)):
            out.append(d)
    return out


def autodetect():
    """Best-guess Logs folder, or None. Prefers dirs that already have logs and
    whose newest log is most recent (i.e. the character you actually play)."""
    cands = _candidates()
    if not cands:
        return None

    def score(d):
        logs = glob.glob(os.path.join(d, "eqlog_*.txt"))
        newest = max((os.path.getmtime(p) for p in logs), default=0)
        return (1 if logs else 0, newest)

    return max(cands, key=score)


# ---------------- top-level resolve ----------------
def resolve(override=None):
    """Return a Logs path (str) or None. Saves an auto-detected path for next time."""
    if override:
        n = normalize(override)
        if n:
            return n
    saved = load().get("logs_dir")
    if saved and is_logs_dir(saved):
        return saved
    auto = autodetect()
    if auto:
        save_logs_dir(auto)
        return auto
    return None
