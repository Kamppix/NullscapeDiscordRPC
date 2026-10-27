"""
Launches the game (common.GAME_NAME) and shows it as your Discord status.

Everything this program reads or writes lives in its own folder (next to the exe):
  icon.ico                   refreshed on every launch if the game icon changed (the icons inside
                             the app exe and uninstall.exe are updated to match)
  <APP_NAME>.log             activity log, only written when started with --log
Nothing is stored in AppData, and no shortcuts are ever created from here (that's the installer's job).
"""

import collections
import glob
import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

import psutil
from pypresence import Presence

import common

# ============================== CONFIG ==============================
DISCORD_CLIENT_ID = "1559974028591239238"
DISPLAY_NAME = common.GAME_NAME   # status headline (needs a recent pypresence)

POLL_SECONDS = 1               # how often to check the game's log (status changes show up this fast)
STARTUP_GRACE_SECONDS = 120     # how long to wait for the game client to appear after launching it
DISCORD_RETRY_SECONDS = 10      # how often to look for Discord when it isn't open
DISCORD_RATE_LIMIT = (5, 20)    # Discord accepts at most 5 status updates per 20 seconds
# ====================================================================

MENU_STATUS = "In the main menu"
WAITING_STATUS = "Waiting for players"
RUN_OVER_STATUS = "Run over"
DISCORD_PROCESS_NAMES = {"discord.exe", "discordptb.exe", "discordcanary.exe", "discorddevelopment.exe"}

FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
PENDING_EXE = os.path.join(APP_DIR, common.APP_EXE + ".new")   # our exe with an updated icon
LOG_FILE = os.path.join(APP_DIR, common.APP_NAME + ".log")
LOG_TO_FILE = "--log" in sys.argv

ROBLOX_LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Roblox", "logs")
PROCESS_NAMES = {"robloxplayerbeta.exe"}

JOIN_PATTERNS = [
    re.compile(r"Joining game '[0-9a-fA-F\-]{36}' place (\d+)"),
    re.compile(r"placeid:(\d+)", re.IGNORECASE),
]
LEAVE_MARKER = "Time to disconnect replication data"
TELEPORT_MARKER = "UgcExperienceController: doTeleport"   # logged right before a teleport's disconnect
PRESENCE_RE = re.compile(r"\[BloxstrapRPC\] (\{.*\})")     # the game's own rich presence messages
LINE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z")

_universe_cache = {}   # place_id -> universe_id


# ------------------------------- Logging -------------------------------

def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    if LOG_TO_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass  # e.g. folder not writable: never let logging break the app
    if sys.stdout:  # None when built with --noconsole
        try:
            print(line, flush=True)
        except Exception:
            pass


def trim_log():
    try:
        if os.path.getsize(LOG_FILE) > 1_000_000:
            os.replace(LOG_FILE, LOG_FILE + ".old")
    except OSError:
        pass


# --------------------------- Roblox web APIs ---------------------------

def place_to_universe(place_id, quiet=False):
    if place_id in _universe_cache:
        return _universe_cache[place_id]
    try:
        data = common.api_get(f"https://apis.roblox.com/universes/v1/places/{place_id}/universe")
        uid = data.get("universeId")
        if uid:
            _universe_cache[place_id] = uid
            log(f"Resolved place {place_id} -> universe {uid}")
            return uid
    except Exception as e:
        if not quiet:
            log(f"Could not resolve place {place_id} to a universe: {e}")
    return None


# --------------------------- Local detection ---------------------------

def find_client(ignore_pids=()):
    """(pid, create_time) of the newest game client process, or None if it isn't running."""
    best = None
    for p in psutil.process_iter(["pid", "name", "create_time"]):
        n = p.info["name"]
        if n and n.lower() in PROCESS_NAMES and p.info["pid"] not in ignore_pids:
            ct = p.info["create_time"] or 0
            if best is None or ct > best[1]:
                best = (p.info["pid"], ct)
    return best


def find_discord():
    """Start time of Discord's main (oldest) process, or None when Discord isn't running.
    Changes only when Discord itself restarts (its helper processes come and go)."""
    times = [p.info["create_time"] for p in psutil.process_iter(["name", "create_time"])
             if (p.info["name"] or "").lower() in DISCORD_PROCESS_NAMES and p.info["create_time"]]
    return min(times) if times else None


def newest_log(min_mtime=None):
    files = glob.glob(os.path.join(ROBLOX_LOG_DIR, "*.log"))
    if min_mtime is not None:
        # Ignore logs from earlier sessions (they may end without a "left game" line)
        files = [f for f in files if os.path.getmtime(f) >= min_mtime - 5]
    return max(files, key=os.path.getmtime) if files else None


def line_timestamp(text, pos):
    line_start = text.rfind("\n", 0, pos) + 1
    m = LINE_TS_RE.match(text[line_start:line_start + 40])
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def is_target_place(place):
    return place == common.PLACE_ID or place_to_universe(place, quiet=True) == common.UNIVERSE_ID


def presence_status(data):
    """(status, difficulty) from the game's own presence data, e.g. ("Level 3", "Casual").
    The game's details read "<game> - <difficulty> <Solo|Multiplayer>"; only the difficulty is
    used, as the Solo/Multiplayer part turned out to be unreliable."""
    state = data.get("state") or ""
    mode = data.get("details") or ""
    prefix = common.GAME_NAME + " - "
    if mode.startswith(prefix):
        mode = mode[len(prefix):]
    if state == "Waiting" and mode == "Standard Solo":
        mode = ""   # the game's placeholder until a new lobby's settings have loaded
    words = mode.split()
    if len(words) > 1 and words[-1] in ("Solo", "Multiplayer"):
        mode = " ".join(words[:-1])
    if state == "Waiting":
        return WAITING_STATUS, mode or None
    return state or None, mode or None


def read_game_state(min_mtime=None):
    """
    (log_path, state) for the client's current visit to the game, or (log_path, None) when it
    isn't in the game. state: {"place", "session_start", "status", "status_since", "mode"}:
      session_start  first join of this visit (teleports between the game's places don't end it)
      status         "In the main menu" / "Waiting for players" / "Level N" / "Run over" (None until known)
      status_since   when status last changed
      mode           e.g. "Casual Multiplayer", or None when unknown
    Everything is rebuilt from the log on each call, so it's also right when we start mid-session.
    """
    path = newest_log(min_mtime)
    if not path:
        return None, None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 3_000_000))
            text = f.read()
    except OSError as e:
        log(f"Could not read log file {path}: {e}")
        return path, None

    events = [(m.start(), "join", int(m.group(1))) for pat in JOIN_PATTERNS for m in pat.finditer(text)]
    events += [(m.start(), "teleport", None) for m in re.finditer(re.escape(TELEPORT_MARKER), text)]
    events += [(m.start(), "leave", None) for m in re.finditer(re.escape(LEAVE_MARKER), text)]
    events += [(m.start(), "presence", m.group(1)) for m in PRESENCE_RE.finditer(text)]
    events.sort(key=lambda e: e[0])

    state = None
    teleporting = False     # a teleport started; its disconnect doesn't mean leaving the game
    disconnected = True     # nothing joined since the last disconnect
    in_run = False          # a level was reported since joining this server
    run_start = None        # when the current run started (for the length shown at "Run over")
    ts = None
    for pos, kind, value in events:
        ts = line_timestamp(text, pos) or ts
        if kind == "teleport":
            teleporting = True
        elif kind == "leave":
            if not teleporting:
                state = None    # a real disconnect: left the game
            teleporting, disconnected = False, True
        elif kind == "join":
            if not disconnected and state and value == state["place"]:
                continue        # the same join reported again (e.g. Roblox's load-time report)
            disconnected = teleporting = False
            if not is_target_place(value):
                state = None
                continue
            if state is None:
                state = {"session_start": ts, "status": None, "status_since": ts, "mode": None}
            state["place"] = value
            in_run = False      # no level seen on this server yet
            # The main menu is the start place. Elsewhere (lobbies and runs) keep showing the
            # previous status and mode until the game reports the new ones a few seconds later.
            if value == common.PLACE_ID:
                if state["status"] != MENU_STATUS:
                    state.update(status=MENU_STATUS, status_since=ts)
                state["mode"] = None
        elif kind == "presence" and state and state["place"] != common.PLACE_ID:
            try:
                payload = json.loads(value)
            except ValueError:
                continue
            if payload.get("command") != "SetRichPresence" or not isinstance(payload.get("data"), dict):
                continue
            status, mode = presence_status(payload["data"])
            if status == WAITING_STATUS and in_run:
                # The game says "Waiting" both in a fresh lobby and when a run has just ended;
                # after a level on the same server it's the latter. Remember how far it got.
                status, mode, in_run = RUN_OVER_STATUS, mode or state["mode"], False
                state["run_result"] = (state["status"], ts - run_start if ts and run_start else None)
            elif status != WAITING_STATUS:
                if not in_run:
                    # A run starts (or we joined one): its start is the game's startTime if given
                    start = payload["data"].get("startTime")
                    run_start = start if isinstance(start, (int, float)) and start > 0 else ts
                    state["run_start"] = run_start
                in_run = True
            if status != state["status"]:
                state.update(status=status, status_since=ts)
            state["mode"] = mode
    return path, state


def in_target_game(client_start):
    """Is the running client currently in the game (not just open, or in another experience)?"""
    return read_game_state(client_start)[1] is not None


def launch_game():
    try:
        os.startfile(f"roblox://experiences/start?placeId={common.PLACE_ID}")
        log("Launched the game.")
    except Exception as e:
        log(f"Could not launch the game: {e}")


def update_exe_icons(ico_path):
    """Put the new icon into uninstall.exe right away, and into our own exe, which is locked while
    running: a patched copy (PENDING_EXE) is moved over it by a helper once we exit."""
    with open(ico_path, "rb") as f:
        ico = f.read()
    uninstaller = os.path.join(APP_DIR, common.UNINSTALL_EXE)
    if os.path.isfile(uninstaller):
        try:
            common.set_exe_icon(uninstaller, ico)
            log("Updated the uninstaller's icon.")
        except Exception as e:
            log(f"Could not update the uninstaller's icon: {e}")
    try:
        common.set_exe_icon(sys.executable, ico, dest=PENDING_EXE)
        common.replace_after_exit(PENDING_EXE, sys.executable)
        log("Updated the app's icon; it takes effect once the app exits.")
    except Exception as e:
        log(f"Could not update the app's icon: {e}")


def update_icon():
    """Runs in the background on every launch; only touches icon.ico (and the exes' icons) if the
    game icon changed."""
    try:
        ico, changed = common.refresh_icon(APP_DIR)
        if FROZEN:
            if changed:
                update_exe_icons(ico)
            elif os.path.isfile(PENDING_EXE):
                # Last time's swap didn't happen (e.g. the app was started again too quickly)
                common.replace_after_exit(PENDING_EXE, sys.executable)
                log("Will apply the app's pending icon update once the app exits.")
        if changed:
            common.notify_icon_change()
    except Exception:
        log("Icon update error (ignored):\n" + traceback.format_exc())


# ------------------------------ Discord ------------------------------

def safe_close(rpc):
    if rpc is None:
        return
    try:
        rpc.close()
    except Exception:
        pass
    try:
        rpc.loop.close()  # avoid leaking an event loop per failed attempt
    except Exception:
        pass


def try_connect():
    rpc = None
    try:
        rpc = Presence(DISCORD_CLIENT_ID)
        rpc.connect()
        return rpc
    except Exception:
        safe_close(rpc)
        return None


def format_duration(seconds):
    """"0min played", "12min played", "1h 2min played", "2h played" (hours only once there are
    any, and no "0min" after them). "Played" rather than "in", as when you join a run in
    progress this is only your part of it."""
    minutes = max(0, int(seconds)) // 60
    hours, minutes = minutes // 60, minutes % 60
    parts = ([f"{hours}h"] if hours else []) + ([f"{minutes}min"] if minutes or not hours else [])
    return " ".join(parts) + " played"


def format_clock(seconds):
    """12:34, or 1:02:05 once there are hours."""
    seconds = max(0, int(seconds))
    hours, minutes, seconds = seconds // 3600, seconds % 3600 // 60, seconds % 60
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def status_lines(game, now):
    """(line 1, line 2) of the activity: the status (levels with how long the run has lasted so far,
    a finished run with how far it got and how long it took), and the mode."""
    line1 = game["status"]
    if line1 == RUN_OVER_STATUS:
        level, length = game.get("run_result") or (None, None)
        line1 = " · ".join([RUN_OVER_STATUS] + ([level] if level else [])
                           + ([format_clock(length)] if length is not None else []))
    elif line1 and line1 not in (MENU_STATUS, WAITING_STATUS):
        # Counted from the run's start, so it keeps going from level to level
        started = game.get("run_start") or game["status_since"] or now
        line1 = f"{line1} · {format_duration(now - started)}"
    return line1, game["mode"]


def set_status(rpc, start_time, line1, line2, quiet=False):
    """Raises on connection problems; the caller treats that as 'Discord went away'.
    Layout: "Playing <game>", line1 and line2 below it, the session timer, and the game icon with
    this app's name on hover (clicking it opens the app's GitHub page)."""
    _, icon = common.fetch_game_info(common.UNIVERSE_ID)
    kwargs = dict(start=start_time, details=line1, state=line2)
    if icon:
        kwargs.update(large_image=icon, large_text=common.APP_NAME)

    try:
        resp = rpc.update(name=DISPLAY_NAME, large_url=common.REPO_URL if icon else None, **kwargs)
        if not quiet:
            log(f"Discord status SET: {DISPLAY_NAME} | {line1} | {line2} | response: {resp}")
    except TypeError:
        resp = rpc.update(**kwargs)
        if not quiet:
            log(f"Discord status SET: {DISPLAY_NAME} | {line1} | {line2} | response: {resp}")
            log("(Old pypresence without name/link support; rebuild with: pip install -U pypresence)")


def shutdown(rpc):
    if rpc is not None:
        try:
            rpc.clear()
        except Exception:
            pass
        safe_close(rpc)
    log("Exiting.")


# -------------------------------- Main --------------------------------

def main(takeover_event):
    log("Starting up.")

    launched_at = None
    client = find_client()
    if client is not None and in_target_game(client[1]):
        log(f"{common.GAME_NAME} is already open in Roblox; not launching it again.")
    else:
        if client is not None:
            log(f"Roblox is open, but not in {common.GAME_NAME}; launching it.")
        launch_game()
        launched_at = time.time()

    threading.Thread(target=update_icon, daemon=True).start()

    started_at = time.time()
    seen_running = False
    dead_pids = set()
    watch_pid, watch_handle = None, None
    prev_running = None
    prev_path = None
    prev_summary = None
    last_err = None

    rpc = None
    last_connect_try = 0.0
    discord_down_logged = False
    discord_started = None   # start time of the Discord we're connected to, to notice restarts
    applied = False          # is our status currently shown on Discord?
    applied_summary = None   # (start, status, mode) last shown, to log real changes only
    applied_shown = None     # (start, line 1, line 2) last sent: we only send when this changes
    recent_updates = collections.deque()   # send times within the rate-limit window
    game = None              # read_game_state() result: None when not in the game
    start_time = None

    while True:
        try:
            now = time.time()
            client = find_client(dead_pids)
            running = client is not None

            if running != prev_running:
                log("Game client is running." if running else "Game client is not running.")
                prev_running = running

            if running:
                pid, client_start = client
                # If we just launched the game while another client was open, that old client may
                # close before the new one appears: only count clients started after the launch.
                if (launched_at is None or client_start >= launched_at - 5
                        or now - started_at > STARTUP_GRACE_SECONDS):
                    seen_running = True
                if pid != watch_pid:
                    common.close_handle(watch_handle)
                    watch_handle = common.open_process_handle(pid)
                    watch_pid = pid
                    if watch_handle is None:
                        log("Couldn't watch the client process directly; falling back to polling.")
            elif seen_running:
                log("Game client closed.")
                shutdown(rpc)
                return
            elif now - started_at > STARTUP_GRACE_SECONDS:
                log("Game client never started; giving up.")
                shutdown(rpc)
                return

            # ---- detect where we are ----
            was_in_game = game is not None
            game = None
            if running:
                path, game = read_game_state(client_start)
                if path != prev_path:
                    if path:
                        log(f"Reading client log: {path}")
                    prev_path = path

            if game is not None:
                session_start = game["session_start"]
                if not (session_start and 0 <= now - session_start < 86400):
                    session_start = start_time if was_in_game and start_time else int(now)
                if session_start != start_time:
                    start_time = session_start
                    log(f"In {common.GAME_NAME} since {time.strftime('%H:%M:%S', time.localtime(start_time))} "
                        f"({int(now - start_time)}s ago).")
                summary = (game["place"], game["status"], game["mode"])
                if summary != prev_summary:
                    log(f"Now: place {summary[0]} | {summary[1] or 'status unknown'} | {summary[2] or 'mode unknown'}")
                    prev_summary = summary
            elif was_in_game:
                log(f"Left {common.GAME_NAME}.")
                prev_summary = None

            # ---- keep Discord in sync (never fatal) ----
            # We only send when something changes, so nothing would reveal a restarted Discord:
            # watch its process instead, and reconnect (and re-send) when it restarts or closes.
            if rpc is not None and find_discord() != discord_started:
                log("Discord was closed or restarted; reconnecting.")
                safe_close(rpc)
                rpc, applied = None, False
                last_connect_try = now - DISCORD_RETRY_SECONDS / 2   # give it a moment to start up

            if rpc is None and now - last_connect_try >= DISCORD_RETRY_SECONDS:
                last_connect_try = now
                rpc = try_connect()
                if rpc is not None:
                    log("Connected to Discord.")
                    discord_started = find_discord()
                    discord_down_logged = False
                    applied = False
                elif not discord_down_logged:
                    log("Discord isn't available; will keep retrying in the background.")
                    discord_down_logged = True

            while recent_updates and now - recent_updates[0] >= DISCORD_RATE_LIMIT[1]:
                recent_updates.popleft()
            if rpc is not None and len(recent_updates) < DISCORD_RATE_LIMIT[0]:
                # Only when what's shown changes (incl. the minutes in "Level 3 · 12min played"); if the rate
                # limit holds an update back, it goes out on a later pass as soon as it's allowed.
                try:
                    if game is not None:
                        line1, line2 = status_lines(game, now)
                        shown = (start_time, line1, line2)
                        if not applied or shown != applied_shown:
                            # Log real changes only, not the minutes ticking up in line 1
                            summary = (start_time, game["status"], game["mode"])
                            set_status(rpc, start_time, line1, line2, quiet=applied and summary == applied_summary)
                            applied, applied_shown, applied_summary = True, shown, summary
                            recent_updates.append(now)
                    elif applied:
                        rpc.clear()
                        applied, applied_shown = False, None
                        recent_updates.append(now)
                        log("Discord status CLEARED.")
                except Exception as e:
                    log(f"Lost connection to Discord ({e.__class__.__name__}); "
                        f"will reconnect when it's available.")
                    safe_close(rpc)
                    rpc, applied = None, False
                    discord_down_logged = True
                    last_connect_try = now

            last_err = None

        except Exception:
            err = traceback.format_exc()
            if err != last_err:  # don't flood the log with the same error
                log("Error in main loop (continuing):\n" + err)
                last_err = err

        # ---- sleep, but wake instantly if a newer instance starts or the client exits ----
        handles = [takeover_event] + ([watch_handle] if watch_handle else [])
        idx = common.wait_any(handles, POLL_SECONDS * 1000)
        if idx == 0:
            log("A newer instance was launched; handing over.")
            shutdown(rpc)
            return
        if idx == 1:  # the client process just ended: re-scan right away
            dead_pids.add(watch_pid)
            common.close_handle(watch_handle)
            watch_handle, watch_pid = None, None
            continue
        if idx == -2:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    common.log_fn = log

    if "--fetch-icon" in sys.argv:
        # Used by build.bat: only downloads icon.png into the current folder (messages go to the console only)
        png = common.download_icon_png()
        if png:
            with open(os.path.join(os.getcwd(), "icon.png"), "wb") as f:
                f.write(png)
        sys.exit(0 if png else 1)

    try:
        if LOG_TO_FILE:
            trim_log()
        event = common.acquire_instance_lock()
        if event is None:
            log("Couldn't take over from the previous instance; exiting.")
            sys.exit(1)
        main(event)
    except SystemExit:
        raise
    except Exception:
        log("Fatal error:\n" + traceback.format_exc())
