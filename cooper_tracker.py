#!/usr/bin/env python3
"""
Cooper Pratt Vestaboard Tracker

Watches Milwaukee Brewers games on MLB's free live data feed and posts
everything Cooper Pratt does to a Vestaboard:
  - lineup spot before the game
  - "now batting" when he steps in
  - the result of every plate appearance (with RBI, season HR count, etc.)
  - stolen bases, caught stealing, runs scored
  - defensive plays he's credited with (putouts, assists, errors)
  - his final line when the game ends

Usage:
  python cooper_tracker.py run            # track today's game(s) (used by GitHub)
  python cooper_tracker.py test           # send a test message to the board
  python cooper_tracker.py preview GAMEPK # print what it WOULD post for a game (no board needed)

Needs one environment variable: VESTABOARD_KEY (your Vestaboard API token).
No extra packages required - standard Python 3.9+ only.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- settings
PLAYER_ID = int(os.environ.get("PLAYER_ID", "806198"))  # Cooper Pratt (MLB.com)
PLAYER_NAME = os.environ.get("PLAYER_NAME", "COOPER PRATT")
TEAM_ID = int(os.environ.get("TEAM_ID", "158"))  # Milwaukee Brewers
LOCAL_TZ = ZoneInfo("America/Chicago")

POLL_SECONDS = 20  # how often to check the live feed
BOARD_GAP_SECONDS = 16  # Vestaboard ignores messages sent closer than ~15s apart
EARLY_START_MINUTES = 60  # start watching this long before first pitch
MAX_RUNTIME = timedelta(hours=5, minutes=40)  # GitHub jobs are capped at 6h

POST_DEFENSE = os.environ.get("POST_DEFENSE", "1") == "1"
POST_NOW_BATTING = os.environ.get("POST_NOW_BATTING", "1") == "1"

MLB = "https://statsapi.mlb.com"
# Current Vestaboard Cloud API, with the older Read/Write API as a fallback
# for keys created before Vestaboard switched systems.
VESTA_ENDPOINTS = [
    ("https://cloud.vestaboard.com/", "X-Vestaboard-Token", lambda g: {"characters": g}),
    ("https://rw.vestaboard.com/", "X-Vestaboard-Read-Write-Key", lambda g: g),
]

# ---------------------------------------------------------------- board encoding
ROWS, COLS = 6, 22
BLUE, YELLOW, WHITE, GREEN, RED = 67, 65, 69, 66, 63

CHAR_CODES = {" ": 0}
for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    CHAR_CODES[c] = i + 1
for i, c in enumerate("123456789"):
    CHAR_CODES[c] = 27 + i
CHAR_CODES.update({
    "0": 36, "!": 37, "@": 38, "#": 39, "$": 40, "(": 41, ")": 42, "-": 44,
    "+": 46, "&": 47, "=": 48, ";": 49, ":": 50, "'": 52, '"': 53, "%": 54,
    ",": 55, ".": 56, "/": 59, "?": 60,
})


def wrap(text, width=COLS):
    """Word-wrap one string into lines no longer than width."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        w = w[:width]
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= width:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def build_board(lines, border=None):
    """Turn a list of text lines into a 6x22 grid of character codes.
    border=(color1, color2) paints the top and bottom rows in alternating tiles."""
    text_rows = []
    for line in lines:
        text_rows.extend(wrap(line.upper()) or [""])
    avail = ROWS - 2 if border else ROWS
    text_rows = text_rows[:avail]

    grid = [[0] * COLS for _ in range(ROWS)]
    first = 1 if border else 0
    offset = first + (avail - len(text_rows)) // 2
    for r, line in enumerate(text_rows):
        start = (COLS - len(line)) // 2
        for c, ch in enumerate(line):
            grid[offset + r][start + c] = CHAR_CODES.get(ch, 0)
    if border:
        for c in range(COLS):
            color = border[c % 2]
            grid[0][c] = color
            grid[ROWS - 1][c] = color
    return grid


def board_preview(grid):
    """Readable text version of a grid, for logs and preview mode."""
    inv = {v: k for k, v in CHAR_CODES.items()}
    tiles = {BLUE: "▓", YELLOW: "▒", WHITE: "□", GREEN: "▒", RED: "▓"}
    out = []
    for row in grid:
        out.append("|" + "".join(inv.get(v, tiles.get(v, "?")) for v in row) + "|")
    return "\n".join(out)


# ---------------------------------------------------------------- network
def get_json(path, tries=3):
    url = path if path.startswith("http") else MLB + path
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cooper-tracker"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception as e:  # network blips happen mid-game; keep going
            print(f"  fetch failed ({e}); retrying", flush=True)
            time.sleep(5 * (attempt + 1))
    return None


class Board:
    def __init__(self, key, dry_run=False):
        self.key = key
        self.dry_run = dry_run
        self.last_sent = 0.0
        self.endpoint = 0

    def post(self, grid):
        print(board_preview(grid), flush=True)
        if self.dry_run:
            print(flush=True)
            return True
        wait = BOARD_GAP_SECONDS - (time.time() - self.last_sent)
        if wait > 0:
            time.sleep(wait)
        endpoints = VESTA_ENDPOINTS[self.endpoint:]
        for n, (url, header, shape) in enumerate(endpoints):
            body = json.dumps(shape(grid)).encode()
            for attempt in range(4):
                req = urllib.request.Request(
                    url, data=body, method="POST",
                    headers={header: self.key, "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=20) as r:
                        print(f"  -> board {r.status}\n", flush=True)
                        self.last_sent = time.time()
                        self.endpoint += n  # remember which API worked
                        return True
                except urllib.error.HTTPError as e:
                    if e.code in (429, 503):  # rate limited: wait and retry
                        time.sleep(BOARD_GAP_SECONDS)
                        continue
                    print(f"  board error {e.code} from {url}: {e.read()[:200]!r}", flush=True)
                    if e.code in (401, 403):
                        break  # try the other API
                    return False
                except Exception as e:
                    print(f"  board error: {e}", flush=True)
                    time.sleep(BOARD_GAP_SECONDS)
        if self.last_sent == 0:
            sys.exit("Vestaboard rejected the key. Check the VESTABOARD_KEY secret.")
        return False


# ---------------------------------------------------------------- game helpers
def ordinal(n):
    n = int(n)
    suf = "TH" if 10 <= n % 100 <= 20 else {1: "ST", 2: "ND", 3: "RD"}.get(n % 10, "TH")
    return f"{n}{suf}"


def inning_label(about):
    half = "TOP" if about.get("halfInning") == "top" else "BOT"
    return f"{half} {ordinal(about.get('inning', 0))}"


def team_abbr(feed, side):
    t = feed["gameData"]["teams"][side]
    return t.get("abbreviation") or t.get("teamName", side)[:3].upper()


def score_line(feed):
    ls = feed["liveData"].get("linescore", {}).get("teams", {})
    a = ls.get("away", {}).get("runs", 0)
    h = ls.get("home", {}).get("runs", 0)
    return f"{team_abbr(feed, 'away')} {a}  {team_abbr(feed, 'home')} {h}"


def opponent_name(feed):
    teams = feed["gameData"]["teams"]
    if teams["home"]["id"] == TEAM_ID:
        return "VS " + teams["away"].get("teamName", "").upper()
    return "AT " + teams["home"].get("teamName", "").upper()


def season_count(description):
    """MLB descriptions include the season total, e.g. 'homers (7)'."""
    m = re.search(r"\((\d+)\)", description or "")
    return m.group(1) if m else None


def hit_detail(description):
    """'... doubles (12) on a line drive to left fielder X.' -> 'LINE DRIVE TO LEFT'"""
    m = re.search(r"on an? ([a-z ]+?) to ([a-z ]+?) (?:fielder|field)", description or "", re.I)
    if m:
        return f"{m.group(1)} to {m.group(2)}".upper()
    return None


BIG_EVENTS = {"Home Run", "Triple", "Grand Slam"}


def batting_message(play, feed):
    res, about = play["result"], play["about"]
    event = res.get("event", "PLAY")
    desc = res.get("description", "")
    rbi = res.get("rbi", 0) or 0

    headline = event.upper()
    if event == "Home Run":
        n = season_count(desc)
        headline = "GRAND SLAM!" if rbi == 4 else "HOME RUN!"
        if n:
            headline += f" #{n}"

    detail = hit_detail(desc) if event != "Strikeout" else None
    if event == "Strikeout":
        detail = "LOOKING" if "called out on strikes" in desc else "SWINGING"
    when = (f"{rbi} RBI - " if rbi else "") + inning_label(about)

    border = (BLUE, YELLOW) if event in BIG_EVENTS or rbi >= 2 else None
    lines = [PLAYER_NAME, headline]
    if detail and not border:  # bordered boards only have 4 text rows
        lines.append(detail)
    lines += [when, score_line(feed)]
    return build_board(lines, border)


def now_batting_message(feed):
    ls = feed["liveData"].get("linescore", {})
    about = feed["liveData"]["plays"]["currentPlay"]["about"]
    offense = ls.get("offense", {})
    on = [b for b, k in (("1ST", "first"), ("2ND", "second"), ("3RD", "third")) if k in offense]
    outs = ls.get("outs", 0)
    situation = f"{outs} OUT" + ("" if outs == 1 else "S")
    runners = ("ON " + " ".join(on)) if on else "BASES EMPTY"
    return build_board(["NOW BATTING", PLAYER_NAME, inning_label(about),
                        situation + " - " + runners, score_line(feed)])


def boxscore_player(feed):
    box = feed["liveData"].get("boxscore", {}).get("teams", {})
    for side in ("home", "away"):
        p = box.get(side, {}).get("players", {}).get(f"ID{PLAYER_ID}")
        if p:
            return side, p
    return None, None


def lineup_message(feed):
    side, p = boxscore_player(feed)
    order = feed["liveData"]["boxscore"]["teams"].get(side or "home", {}).get("battingOrder", [])
    if not side or PLAYER_ID not in order:
        return None
    spot = order.index(PLAYER_ID) + 1
    pos = (p.get("position") or {}).get("abbreviation", "SS")
    return build_board(["TODAY'S LINEUP", PLAYER_NAME, f"{pos} - BATTING {ordinal(spot)}",
                        opponent_name(feed)], (BLUE, YELLOW))


def lineup_posted(feed):
    box = feed["liveData"].get("boxscore", {}).get("teams", {})
    for side in ("home", "away"):
        if feed["gameData"]["teams"][side]["id"] == TEAM_ID:
            return bool(box.get(side, {}).get("battingOrder"))
    return False


def final_message(feed):
    _, p = boxscore_player(feed)
    lines = ["FINAL: " + score_line(feed)]
    b = (p or {}).get("stats", {}).get("batting", {})
    if b.get("plateAppearances") or b.get("atBats"):
        line = f"{b.get('hits', 0)} FOR {b.get('atBats', 0)}"
        for key, label in (("homeRuns", "HR"), ("triples", "3B"), ("doubles", "2B")):
            n = b.get(key, 0)
            if n:
                line += " " + (label if n == 1 else f"{n} {label}")
        extras = []
        if b.get("rbi"):
            extras.append(f"{b['rbi']} RBI")
        if b.get("runs"):
            extras.append(f"{b['runs']} R")
        if b.get("baseOnBalls"):
            extras.append(f"{b['baseOnBalls']} BB")
        if b.get("stolenBases"):
            extras.append(f"{b['stolenBases']} SB")
        lines += [PLAYER_NAME, line] + ([" ".join(extras)] if extras else [])
    else:
        lines += [PLAYER_NAME, "DID NOT PLAY"]
    return build_board(lines, (BLUE, YELLOW))


# ---------------------------------------------------------------- the tracker
class GameTracker:
    def __init__(self, game_pk, board):
        self.game_pk = game_pk
        self.board = board
        self.seen = set()
        self.lineup_done = False
        self.caught_up = False

    def fetch(self):
        return get_json(f"/api/v1.1/game/{self.game_pk}/feed/live")

    def events(self, feed):
        """Yield (key, grid) for every Cooper event in the feed."""
        plays = feed["liveData"]["plays"].get("allPlays", [])
        for play in plays:
            idx = play["about"].get("atBatIndex")
            complete = play["about"].get("isComplete")
            is_batter = play.get("matchup", {}).get("batter", {}).get("id") == PLAYER_ID

            # plate appearance result
            if is_batter and complete:
                yield f"ab-{idx}", batting_message(play, feed)

            # baserunning and defense (these can happen during someone else's at-bat)
            for r in play.get("runners", []):
                d = r.get("details", {})
                ev = d.get("event", "") or ""
                if (d.get("runner") or {}).get("id") == PLAYER_ID:
                    key = f"run-{idx}-{d.get('playIndex')}-{d.get('eventType')}"
                    if "Stolen Base" in ev:
                        base = ev.replace("Stolen Base", "").strip() or ""
                        yield key, build_board([PLAYER_NAME, f"STOLEN BASE! {base}",
                                                inning_label(play["about"]), score_line(feed)],
                                               (BLUE, YELLOW))
                    elif "Caught Stealing" in ev or "Pickoff" in ev:
                        yield key, build_board([PLAYER_NAME, ev.upper(),
                                                inning_label(play["about"])])
                    elif (r.get("movement") or {}).get("end") == "score" and not (
                            is_batter and play["result"].get("event") == "Home Run"):
                        yield f"score-{idx}", build_board(
                            [PLAYER_NAME, "SCORES!", inning_label(play["about"]),
                             score_line(feed)])

                if POST_DEFENSE and complete and not is_batter:
                    for credit in r.get("credits", []):
                        if (credit.get("player") or {}).get("id") != PLAYER_ID:
                            continue
                        kind = credit.get("credit", "")
                        if "error" in kind:
                            label = "ERROR"
                        elif kind in ("f_assist", "f_putout", "f_fielded_ball"):
                            label = "DEFENSE"
                        else:
                            continue
                        yield f"def-{idx}", build_board(
                            [PLAYER_NAME, label, play["result"].get("event", "").upper(),
                             inning_label(play["about"])])

        # "now batting"
        if POST_NOW_BATTING:
            cur = feed["liveData"]["plays"].get("currentPlay") or {}
            if (cur.get("matchup", {}).get("batter", {}).get("id") == PLAYER_ID
                    and not cur.get("about", {}).get("isComplete")):
                yield f"up-{cur['about'].get('atBatIndex')}", now_batting_message(feed)

    def step(self, feed):
        """Process one feed snapshot. Returns True when the game is over."""
        state = feed["gameData"]["status"]["abstractGameState"]

        if not self.caught_up:
            # Joined a game already in progress: don't replay old plays.
            if state == "Live":
                for key, _ in self.events(feed):
                    if not key.startswith("up-"):
                        self.seen.add(key)
                self.lineup_done = True
            self.caught_up = True

        if not self.lineup_done and lineup_posted(feed):
            self.lineup_done = True
            msg = lineup_message(feed)
            self.board.post(msg or build_board([PLAYER_NAME, "NOT IN TODAY'S LINEUP",
                                                opponent_name(feed)]))

        for key, grid in self.events(feed):
            if key not in self.seen:
                self.seen.add(key)
                self.board.post(grid)

        if state == "Final":
            if "final" not in self.seen:
                self.seen.add("final")
                self.board.post(final_message(feed))
            return True
        return False

    def run(self, deadline):
        print(f"Tracking game {self.game_pk}", flush=True)
        while datetime.now(timezone.utc) < deadline:
            feed = self.fetch()
            if feed:
                try:
                    if self.step(feed):
                        return True
                except (KeyError, TypeError) as e:
                    print(f"  skipped a malformed update: {e}", flush=True)
            time.sleep(POLL_SECONDS)
        return False


def todays_games(day):
    data = get_json(f"/api/v1/schedule?sportId=1&teamId={TEAM_ID}&date={day:%Y-%m-%d}")
    games = []
    for d in (data or {}).get("dates", []):
        for g in d.get("games", []):
            detailed = g["status"].get("detailedState", "")
            if detailed in ("Postponed", "Cancelled", "Suspended"):
                continue
            start = datetime.fromisoformat(g["gameDate"].replace("Z", "+00:00"))
            games.append((start, g["gamePk"], g["status"]["abstractGameState"]))
    return sorted(games)


def cmd_run(board):
    started = datetime.now(timezone.utc)
    deadline = started + MAX_RUNTIME
    day = datetime.now(LOCAL_TZ).date()
    games = todays_games(day)
    if not games:
        print(f"No Brewers game on {day}.")
        return
    for start, pk, state in games:
        if state == "Final":
            continue
        if start - datetime.now(timezone.utc) > timedelta(minutes=EARLY_START_MINUTES):
            print(f"Next game ({pk}) starts {start.astimezone(LOCAL_TZ):%I:%M %p}; "
                  "a later check will pick it up.")
            return
        # wait for first pitch window if we're early
        GameTracker(pk, board).run(deadline)
    print("Done for today.")


def cmd_preview(game_pk):
    feed = get_json(f"/api/v1.1/game/{game_pk}/feed/live")
    if not feed:
        sys.exit("Couldn't load that game.")
    t = GameTracker(game_pk, Board("", dry_run=True))
    t.caught_up = True  # show the whole game, not just new plays
    t.step(feed)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "preview":
        if len(sys.argv) < 3:
            sys.exit("usage: cooper_tracker.py preview GAMEPK")
        cmd_preview(sys.argv[2])
        return

    key = os.environ.get("VESTABOARD_KEY", "").strip()
    if not key:
        sys.exit("Set VESTABOARD_KEY to your Vestaboard API token.")
    board = Board(key)

    if cmd == "test":
        board.post(build_board(["COOPER PRATT TRACKER", "IS CONNECTED", "GO BREWERS!"],
                               (BLUE, YELLOW)))
    elif cmd == "run":
        cmd_run(board)
    else:
        sys.exit(f"unknown command {cmd!r}")


if __name__ == "__main__":
    main()
