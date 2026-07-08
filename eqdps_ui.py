#!/usr/bin/env python3
"""
SlopMeter - always-on-top overlay DPS/HPS meter for EverQuest (live / Legends / emu).

Reuses the parser/attribution from eqdps.py (same numbers as the console meter),
and paints a compact, draggable, semi-transparent overlay you can float over the
game. No third-party packages - pure Tkinter (bundled with Python).

Run:  python eqdps_ui.py     (or double-click SlopMeter.bat)
Controls:
    * drag the top bar to move it       * right-click title = change EQ folder
    * DPS / HPS toggles the table       * ⧉ copies the parse to the clipboard
    * R resets the current encounter     * AP arms in-game auto-post (#dps -> /g)
    * 📌 toggles always-on-top           * ✕ closes
"""

import os
import time
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

import eqdps as core   # parse_line, Encounter, route, is_mob, newest_log, char_name_from_path
import eqchat          # posts a parse into EQ group chat via clipboard + Ctrl+V
import eqfind          # auto-detects / remembers the EverQuest Logs folder

# In-game triggers: type these in any chat channel (e.g. a hotbutton -> /say #dps).
# The meter sees the line in the log and auto-posts the parse to the group.
CHANNEL = "/g "                         # where the parse is posted (/g = group)
TRIGGERS = {"#dps": "dmg", "#hps": "heal", "#parse": None}  # None -> current view
POST_COOLDOWN = 2.0                     # seconds between auto-posts (anti-spam)

# ---- look & feel ----
W, ROWS, ROW_H = 330, 8, 26
BG      = "#12141a"
BAR_BG  = "#1d212b"
FG      = "#e8eaed"
MUTED   = "#8a90a0"
GOLD    = "#ffc63c"          # you
DMG_PAL = ["#e0554e", "#e08a3c", "#d7c04a", "#6fb04a", "#4a9fd7", "#8a6fe0", "#d76fb0", "#4ac0b0"]
HEAL    = "#5cba63"
TAKEN   = "#e0554e"
REFRESH_MS = 250
IDLE_RESET = 10.0


class Meter(tk.Tk):
    def __init__(self, logs_dir=None):
        super().__init__()
        self.logs_dir = logs_dir or eqfind.resolve()
        self.mode = "dmg"                 # "dmg" or "heal"
        self.pinned = True
        self.autopost = False             # in-game trigger -> post to group (off by default)
        self.last_post = 0.0
        self.flash = ""                   # transient footer message
        self.flash_until = 0.0
        self.enc = core.Encounter()
        self.mobs = set()
        self.me = "You"
        self.logfile = None
        self.f = None
        self.last_event = 0.0

        # frameless, translucent, always-on-top overlay
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.92)
        self.configure(bg=BG)
        h = 30 + ROWS * ROW_H + 34
        self.geometry(f"{W}x{h}+120+120")

        self._build_topbar()
        self.canvas = tk.Canvas(self, width=W, height=ROWS * ROW_H,
                                bg=BG, highlightthickness=0)
        self.canvas.pack(fill="x")
        self.footer = tk.Label(self, text="", bg=BG, fg=MUTED, anchor="w",
                               font=("Consolas", 9), padx=8)
        self.footer.pack(fill="x")

        if not self._ensure_logs_dir():
            self.after(0, self.destroy)   # user cancelled the picker
            return
        self._open_newest()
        self.after(REFRESH_MS, self._tick)

    # ---------- first-run / folder selection ----------
    def _ensure_logs_dir(self):
        if self.logs_dir and eqfind.is_logs_dir(self.logs_dir):
            return True
        return self._pick_folder(first_run=True)

    def _pick_folder(self, first_run=False):
        if first_run:
            messagebox.showinfo(
                "SlopMeter setup",
                "Couldn't auto-detect your EverQuest folder.\n\n"
                "Pick your EverQuest install folder (or its Logs folder).\n"
                "Then enable logging in-game with:  /log on")
        picked = filedialog.askdirectory(title="Select your EverQuest (or Logs) folder")
        logs = eqfind.normalize(picked) if picked else None
        if logs:
            self.logs_dir = logs
            eqfind.save_logs_dir(logs)
            self._open_newest()
            self._notify("logs folder set")
            return True
        if first_run:
            messagebox.showwarning("SlopMeter",
                                   "That didn't look like an EverQuest folder. Exiting.")
        return False

    # ---------- top bar ----------
    def _build_topbar(self):
        bar = tk.Frame(self, bg="#0c0e13", height=30)
        bar.pack(fill="x")
        self.title_lbl = tk.Label(bar, text="SlopMeter", bg="#0c0e13", fg=GOLD,
                                  font=("Segoe UI Semibold", 10), padx=8)
        self.title_lbl.pack(side="left")
        self.title_lbl.bind("<Button-3>", lambda e: self._pick_folder())  # right-click = change folder

        def btn(txt, cmd, fg=FG, w=3):
            b = tk.Label(bar, text=txt, bg="#0c0e13", fg=fg, width=w,
                         font=("Segoe UI", 9, "bold"), cursor="hand2")
            b.pack(side="right", padx=1)
            b.bind("<Button-1>", lambda e: (cmd(), "break"))
            return b

        btn("✕", self.destroy, fg="#e0776f", w=2)
        self.pin_btn = btn("📌", self._toggle_pin, w=2)
        btn("R", self._reset, w=2)
        self.copy_btn = btn("⧉", self._copy, fg="#9fe0a0", w=2)
        self.mode_btn = btn("DPS", self._toggle_mode, fg="#7fb2ff", w=4)
        self.ap_btn = btn("AP", self._toggle_autopost, fg=MUTED, w=3)  # auto-post to /g

        # drag to move (bind on the bar and the static title)
        for w in (bar, self.title_lbl):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

    def _drag_start(self, e):
        self._dx, self._dy = e.x, e.y

    def _drag_move(self, e):
        self.geometry(f"+{self.winfo_pointerx() - self._dx}+{self.winfo_pointery() - self._dy}")

    def _toggle_mode(self):
        self.mode = "heal" if self.mode == "dmg" else "dmg"
        self.mode_btn.config(text="HPS" if self.mode == "heal" else "DPS")

    def _toggle_pin(self):
        self.pinned = not self.pinned
        self.attributes("-topmost", self.pinned)
        self.pin_btn.config(fg=FG if self.pinned else MUTED)

    def _reset(self):
        self.enc = core.Encounter()
        self.last_event = 0.0

    # ---------- copyable parse for EQ chat ----------
    def _parse_text(self, mode=None):
        """One-line summary of a table, ready to paste into EQ chat.
        (EQ's chat input is a single line, so this is intentionally one line.)"""
        mode = mode or self.mode
        data = self.enc.dmg if mode == "dmg" else self.enc.heal
        dur = self.enc.duration
        total = sum(data.values())
        if not total:
            return "No combat parsed yet."
        label = "DPS" if mode == "dmg" else "HPS"
        ranked = sorted(data.items(), key=lambda kv: kv[1], reverse=True)[:8]
        parts = [f"{i}) {src} {val/dur:,.0f} ({100*val/total:.0f}%)"
                 for i, (src, val) in enumerate(ranked, 1)]
        head = f"{label} {dur:.0f}s"
        if mode == "dmg":
            tgt = max((n for n in self.enc.taken if core.is_mob(n, self.mobs)),
                      key=lambda n: self.enc.taken[n], default=None)
            if tgt:
                head += f" vs {tgt}"
        line = f"{head} | " + "  ".join(parts) + f" | raid {total/dur:,.0f}/s"
        # keep it inside EQ's chat input limit; trim trailing entries if needed
        while len(line) > 500 and len(parts) > 3:
            parts.pop()
            line = f"{head} | " + "  ".join(parts) + f" | raid {total/dur:,.0f}/s"
        return line

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self._parse_text())
        self.update()                     # push to the OS clipboard
        self.copy_btn.config(text="✓")
        self.after(900, lambda: self.copy_btn.config(text="⧉"))

    # ---------- in-game trigger -> auto-post to group ----------
    def _toggle_autopost(self):
        self.autopost = not self.autopost
        self.ap_btn.config(fg="#5cba63" if self.autopost else MUTED)
        self._notify("auto-post ON — /say #dps in game" if self.autopost
                     else "auto-post OFF")

    def _notify(self, msg, secs=2.5):
        self.flash, self.flash_until = msg, time.time() + secs

    def _check_trigger(self, line):
        """If YOU typed a trigger token in chat, return the mode to post, else None."""
        if "] You " not in line:          # only lines you emitted
            return False
        low = line.lower()
        for tok, mode in TRIGGERS.items():
            if tok in low:
                return mode or self.mode   # #parse -> whatever's on screen
        return False

    def _autopost(self, mode):
        now = time.time()
        if now - self.last_post < POST_COOLDOWN:
            return
        parse = self._parse_text(mode)
        if parse.startswith("No combat"):
            self._notify("nothing to post yet")
            return
        self.last_post = now
        saved = None
        try:
            saved = self.clipboard_get()
        except tk.TclError:
            pass
        self.clipboard_clear()
        self.clipboard_append(CHANNEL + parse)
        self.update()
        self._notify(f"posting {('DPS' if mode=='dmg' else 'HPS')} to group…")

        def worker():
            ok = eqchat.post()
            self.after(0, lambda: self._notify("posted to /g" if ok
                                               else "EQ window not found"))
            if saved is not None:          # restore prior clipboard
                self.after(1500, lambda: (self.clipboard_clear(),
                                          self.clipboard_append(saved), self.update()))
        threading.Thread(target=worker, daemon=True).start()

    # ---------- log tailing ----------
    def _open_newest(self):
        nl = core.newest_log(self.logs_dir)
        if nl and nl != self.logfile:
            if self.f:
                self.f.close()
            self.logfile = nl
            self.me = core.char_name_from_path(nl)
            self.mobs = set()
            self.enc = core.Encounter()
            self.f = open(nl, "r", encoding="utf-8", errors="ignore")
            self.f.seek(0, os.SEEK_END)
            self.title_lbl.config(text=f"SlopMeter — {self.me}")

    def _pump(self):
        if not self.f:
            return
        while True:
            line = self.f.readline()
            if not line:
                break
            if self.autopost:
                mode = self._check_trigger(line)
                if mode:
                    self._autopost(mode)
                    continue
            r = core.parse_line(line, self.me)
            if not r:
                continue
            metric, ts, src, tgt, amt = r
            if not src:
                continue
            now = time.time()
            if self.last_event and (now - self.last_event) > IDLE_RESET:
                self.enc = core.Encounter()      # new pull after idle gap
            self.last_event = now
            core.route(self.enc, self.mobs, metric, ts, src, tgt, amt, self.me)

    def _tick(self):
        try:
            self._pump()
            self._open_newest()          # follow relog / new char
        except (OSError, ValueError):
            pass
        self._redraw()
        self.after(REFRESH_MS, self._tick)

    # ---------- painting ----------
    def _redraw(self):
        c = self.canvas
        c.delete("all")
        data = self.enc.dmg if self.mode == "dmg" else self.enc.heal
        dur = self.enc.duration
        total = sum(data.values())
        ranked = sorted(data.items(), key=lambda kv: kv[1], reverse=True)[:ROWS]
        top = ranked[0][1] if ranked else 1

        if not ranked:
            c.create_text(W // 2, ROWS * ROW_H // 2, fill=MUTED,
                          font=("Segoe UI", 10),
                          text="waiting for combat…  (/log on)")
        for i, (src, val) in enumerate(ranked):
            y0 = i * ROW_H + 2
            y1 = y0 + ROW_H - 4
            frac = val / top if top else 0
            is_me = (src == self.me)
            if self.mode == "heal":
                color = HEAL
            else:
                color = GOLD if is_me else DMG_PAL[i % len(DMG_PAL)]
            c.create_rectangle(6, y0, W - 6, y1, fill=BAR_BG, outline="")
            c.create_rectangle(6, y0, 6 + (W - 12) * frac, y1, fill=color, outline="")
            name = ("▶ " if is_me else "") + src[:22]
            c.create_text(12, (y0 + y1) // 2, anchor="w", fill="#0c0e13" if frac > 0.5 else FG,
                          font=("Segoe UI Semibold", 9), text=name)
            per = val / dur
            pct = 100 * val / total if total else 0
            c.create_text(W - 10, (y0 + y1) // 2, anchor="e", fill="#0c0e13" if frac > 0.9 else FG,
                          font=("Consolas", 9),
                          text=f"{per:,.0f}/s  {pct:4.1f}%")

        if self.flash and time.time() < self.flash_until:
            self.footer.config(text=self.flash, fg="#9fe0a0")
            return
        mt = self.enc.taken.get(self.me, 0)
        rate = total / dur
        self.footer.config(
            fg=MUTED,
            text=f"{dur:4.0f}s   {'DPS' if self.mode=='dmg' else 'HPS'} {rate:,.0f}/s   "
                 f"│  you took {mt/dur:,.0f}/s")


if __name__ == "__main__":
    Meter().mainloop()
