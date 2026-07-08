#!/usr/bin/env python3
"""
SlopMeter (console) - a live combat meter for EverQuest (live / Legends / any emu).

Tracks, per source, over the current encounter:
    * DPS  - damage done (players/pets only; mobs are excluded)
    * HPS  - healing done
    * DTPS - damage taken (tanking), shown for you

How it works
------------
EQ has no in-client addon runtime, so this is an EXTERNAL parser: it tails the
newest combat log the client writes and prints a refreshing table. Enable logging
in-game once with:  /log on

Usage
-----
    python eqdps.py                       # auto-detect your EQ Logs folder
    python eqdps.py "C:\\path\\to\\eqlog_Name_server.txt"
    python eqdps.py --logs "C:\\path\\to\\Logs"   # watch a Logs dir, auto-follow newest file
    python eqdps.py --idle 12             # seconds of no combat before an encounter resets (default 10)
"""

import argparse
import os
import re
import sys
import time
import glob
from collections import defaultdict

import eqfind   # auto-detects / remembers the EverQuest Logs folder

# EQ timestamp: [Wed Jul 08 12:34:56 2026]
TS_RE = re.compile(r"^\[[A-Za-z]{3} ([A-Za-z]{3}) +(\d+) (\d+):(\d+):(\d+) (\d+)\] (.*)$")
MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), start=1)}

NUM = r"([\d,]+)"

# Finite set of EQ melee/attack verbs. Using a whitelist (rather than \w+) lets the
# non-greedy attacker group swallow multi-word mob names correctly, e.g.
# "An ire ghast hits Rude" -> attacker="An ire ghast", verb="hits", target="Rude".
VERBS = (r"(?:frenzies on|frenzy on|hit|hits|slash|slashes|crush|crushes|pierce|pierces|"
         r"bash|bashes|kick|kicks|bite|bites|claw|claws|gore|gores|maul|mauls|punch|punches|"
         r"slam|slams|sting|stings|smash|smashes|strike|strikes|backstab|backstabs|"
         r"rend|rends|slice|slices|pummel|pummels|slap|slaps|mangle|mangles|gouge|gouges|"
         r"smite|smites|cleave|cleaves|reave|reaves|slay|slays|flurries|gnaw|gnaws)")

# metric tags returned by parse_line:
#   "melee"    - generic hit; caller decides damage-vs-taken via is_mob(source)
#   "dmg"      - your/a player's DoT tick (always a damage credit)
#   "selftaken"- a DoT ticking on you (always your damage taken)
#   "heal"     - healing done
PATTERNS = [
    # First-person skill proc:  Your kick hits a loathling lich for 27 points of damage.
    # (matched before generic melee so the skill word isn't mistaken for the verb)
    ("selfmelee", re.compile(rf"^Your [\w' ]+? hits? (?P<target>.+?) for {NUM}(?: \([\d,]+\))? point[s]? of (?:[\w-]+ )?damage")),
    # Generic melee (any attacker, incl. You/YOU and multi-word mobs):
    ("melee",     re.compile(rf"^(?P<attacker>.+?) {VERBS} (?P<target>.+?) for {NUM}(?: \([\d,]+\))? point[s]? of (?:[\w-]+ )?damage")),
    # DoT / spell tick with a source:  An abhorrent has taken 27 damage from your Selo's Chords.
    ("dmg",       re.compile(rf"^(?P<target>.+?) has taken {NUM} damage from (?:your|(?P<attacker>[\w`'-]+)'s) ")),
    # DoT ticking on you:  You have taken 40 damage from Golem Rot.
    ("selftaken", re.compile(rf"^You have taken {NUM} damage from ")),
    # First-person heal:  You have healed Groknar for 500 hit points.
    ("selfheal",  re.compile(rf"^You (?:have )?healed (?P<target>.+?) for {NUM}(?: \([\d,]+\))? (?:hit )?point[s]?")),
    # Third-person heal:  Jido healed Janer for 313 hit points by Symbol of Pinzarn.
    ("heal",      re.compile(rf"^(?P<attacker>[A-Z][\w`'-]+) (?:has |have )?healed (?P<target>.+?) for {NUM}(?: \([\d,]+\))? (?:hit )?point[s]?")),
    # HoT tick w/ source:  Groknar has been healed for 120 points by your Renewal.
    ("heal",      re.compile(rf"^(?P<target>.+?) (?:has|have) been healed for {NUM}(?: \([\d,]+\))? (?:hit )?point[s]? by (?:your|(?P<attacker>[\w`'-]+)'s) ")),
]


def _amount(g):
    """The captured group that is purely digits/commas is the damage/heal amount."""
    for val in g.groups():
        if val and re.fullmatch(r"[\d,]+", val):
            return int(val.replace(",", ""))
    return 0


def _norm(name, me):
    return me if name in ("You", "YOU", "Your") else name


def parse_line(line, me):
    """Return (metric, ts, source, target, amount) or None. metric is one of
    'melee' | 'dmg' | 'selftaken' | 'heal'."""
    m = TS_RE.match(line)
    if not m:
        return None
    mon, day, hh, mm, ss, yr = m.groups()[:6]
    body = m.group(7)
    try:
        ts = time.mktime((int(yr), MONTHS[mon], int(day), int(hh), int(mm), int(ss), 0, 0, -1))
    except (KeyError, ValueError):
        return None

    for kind, rx in PATTERNS:
        g = rx.match(body)
        if not g:
            continue
        gd = g.groupdict()
        amount = _amount(g)
        target = _norm((gd.get("target") or "?").strip(), me)

        if kind == "selfmelee":
            return "melee", ts, me, target, amount
        if kind == "selfheal":
            return "heal", ts, me, target, amount
        if kind == "selftaken":
            return "selftaken", ts, me, me, amount
        # melee / dmg / heal with an optional named attacker ("your"/None => you)
        src = gd.get("attacker")
        src = me if src in (None, "You", "YOU") else src
        return kind, ts, _norm(src, me), target, amount
    return None


class Encounter:
    def __init__(self):
        self.dmg = defaultdict(int)    # source -> damage done (players/pets)
        self.heal = defaultdict(int)   # source -> healing done
        self.taken = defaultdict(int)  # target -> damage taken
        self.start = None
        self.last = None

    def _touch(self, ts):
        if self.start is None:
            self.start = ts
        self.last = ts

    def damage(self, ts, src, tgt, amt):
        self._touch(ts); self.dmg[src] += amt; self.taken[tgt] += amt

    def incoming(self, ts, tgt, amt):
        self._touch(ts); self.taken[tgt] += amt

    def healing(self, ts, src, amt):
        self._touch(ts); self.heal[src] += amt

    @property
    def duration(self):
        # never 0 -> avoids divide-by-zero in the rate columns before combat starts
        if self.start is None:
            return 1.0
        return max(1.0, (self.last or self.start) - self.start)


def is_mob(name, mobs):
    # EQ player names are always a single word with no spaces, so anything
    # containing a space (or article-prefixed, or learned) is a mob/NPC.
    n = name.lower()
    return (" " in name) or n.startswith(("a ", "an ", "the ")) or n in mobs


def char_name_from_path(path):
    m = re.search(r"eqlog_([^_]+)_", os.path.basename(path))
    return m.group(1) if m else "You"


def newest_log(logs_dir):
    files = glob.glob(os.path.join(logs_dir, "eqlog_*.txt"))
    return max(files, key=os.path.getmtime) if files else None


def _table(title, data, dur, me, top=8):
    total = sum(data.values())
    print(f"  {title}   total {total:,}   rate {total/dur:,.0f}/s")
    ranked = sorted(data.items(), key=lambda kv: kv[1], reverse=True)[:top]
    if not ranked:
        print("    (none)")
    for src, amt in ranked:
        pct = 100.0 * amt / total if total else 0
        star = "*" if src == me else " "
        print(f"   {star}{src[:20]:<20} {amt:>10,} {amt/dur:>8,.0f}/s {pct:>5.1f}%")
    print()


def render(enc, me, logfile):
    os.system("cls" if os.name == "nt" else "clear")
    dur = enc.duration
    print(f"  eqdps  |  {os.path.basename(logfile)}  |  you = {me}  |  encounter {dur:5.1f}s")
    print("  " + "=" * 54)
    if not enc.dmg and not enc.heal and not enc.taken:
        print("  (waiting for combat... make sure /log on is set)\n")
    _table("DPS  (damage done)", enc.dmg, dur, me)
    _table("HPS  (healing done)", enc.heal, dur, me)
    mt = enc.taken.get(me, 0)
    print(f"  DTPS (you took)      {mt:>10,} {mt/dur:>8,.0f}/s")
    print("\n  Ctrl+C to quit")


def route(enc, mobs, metric, ts, src, tgt, amt, me):
    """Apply one parsed event to the encounter, deciding damage-vs-taken."""
    if metric == "heal":
        enc.healing(ts, src, amt)
    elif metric == "selftaken":
        enc.incoming(ts, me, amt)
    elif metric == "dmg":                 # DoT by you / a player
        enc.damage(ts, src, tgt, amt)
    elif metric == "melee":
        if src == me:
            mobs.add(tgt.lower())         # learn: things you attack are mobs
        if is_mob(src, mobs):             # mob hitting someone -> incoming
            enc.incoming(ts, tgt, amt)
        else:                             # player/pet dealing damage
            enc.damage(ts, src, tgt, amt)


def main():
    ap = argparse.ArgumentParser(description="SlopMeter - live combat meter for EverQuest.")
    ap.add_argument("logfile", nargs="?", help="specific eqlog_*.txt to tail")
    ap.add_argument("--logs", default=None, help="EQ Logs dir (overrides auto-detect)")
    ap.add_argument("--idle", type=float, default=10.0, help="idle seconds before encounter resets")
    args = ap.parse_args()

    logs_dir = None
    if args.logfile:
        logfile = args.logfile
    else:
        logs_dir = eqfind.resolve(args.logs)
        if not logs_dir:
            print("SlopMeter couldn't find your EverQuest Logs folder.\n"
                  "  Point it there explicitly, e.g.:\n"
                  '    python eqdps.py --logs "C:\\...\\EverQuest\\Logs"')
            sys.exit(1)
        logfile = newest_log(logs_dir)
    if not logfile or not os.path.exists(logfile):
        print(f"No eqlog file found yet in:\n  {logs_dir}\n"
              f"  - In-game type: /log on (while logged into a character), then relaunch.")
        sys.exit(1)

    me = char_name_from_path(logfile)
    mobs = set()
    enc = Encounter()
    last_event_wall = 0.0
    print(f"Tailing {logfile}\nYou = {me}. Waiting for combat...")

    f = open(logfile, "r", encoding="utf-8", errors="ignore")
    f.seek(0, os.SEEK_END)
    last_render = 0.0

    try:
        while True:
            line = f.readline()
            if line:
                res = parse_line(line, me)
                if res:
                    metric, ts, src, tgt, amt = res
                    if not src:
                        continue
                    now = time.time()
                    if last_event_wall and (now - last_event_wall) > args.idle:
                        enc = Encounter()          # idle gap -> new encounter
                    last_event_wall = now
                    route(enc, mobs, metric, ts, src, tgt, amt, me)
                continue

            # no new line: refresh display, watch for rotation / newer file
            now = time.time()
            if now - last_render > 0.5:
                render(enc, me, logfile)
                last_render = now
            if not args.logfile and logs_dir:
                nl = newest_log(logs_dir)
                if nl and nl != logfile:
                    f.close()
                    logfile, me, mobs = nl, char_name_from_path(nl), set()
                    enc = Encounter()
                    f = open(logfile, "r", encoding="utf-8", errors="ignore")
                    f.seek(0, os.SEEK_END)
                    continue
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        f.close()


if __name__ == "__main__":
    main()
