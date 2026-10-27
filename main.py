"""
Launches the game (common.GAME_NAME) and shows it as your Discord status.

Everything this program reads or writes lives in its own folder (next to the exe):
  icon.ico                   refreshed on every launch if the game icon changed (the icons inside
                             the app exe and uninstall.exe are updated to match)
  <APP_NAME>.log             activity log, only written when started with --log
Nothing is stored in AppData, and no shortcuts are ever created from here (that's the installer's job).
"""

import glob
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

POLL_SECONDS = 3
STARTUP_GRACE_SECONDS = 120     # how long to wait for the game client to appear after launching it
DISCORD_RETRY_SECONDS = 10      # how often to look for Discord when it isn't open
KEEPALIVE_SECONDS = 30          # re-send status this often (also detects a dead Discord pipe)
# ====================================================================

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


def detect_current_place(min_mtime=None):
    """Return (log_path, place_id_or_None, join_epoch_or_None)."""
    path = newest_log(min_mtime)
    if not path:
        return None, None, None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 3_000_000))
            text = f.read()
    except OSError as e:
        log(f"Could not read log file {path}: {e}")
        return path, None, None

    best_pos, place = -1, None
    for pat in JOIN_PATTERNS:
        for m in pat.finditer(text):
            if m.start() > best_pos:
                best_pos, place = m.start(), int(m.group(1))

    if place is None:
        return path, None, None
    if text.rfind(LEAVE_MARKER) > best_pos:
        return path, None, None
    return path, place, line_timestamp(text, best_pos)


def in_target_game(client_start):
    """Is the running client currently in the game (not just open, or in another experience)?"""
    _, place, _ = detect_current_place(client_start)
    if not place:
        return False
    if place == common.PLACE_ID:
        return True
    return place_to_universe(place) == common.UNIVERSE_ID


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


def set_status(rpc, start_time, quiet=False):
    """Raises on connection problems; the caller treats that as 'Discord went away'."""
    _, icon = common.fetch_game_info(common.UNIVERSE_ID)
    kwargs = dict(start=start_time)  # add details="...", large_text="..." here if you want them
    if icon:
        kwargs["large_image"] = icon

    try:
        resp = rpc.update(name=DISPLAY_NAME, **kwargs)
        if not quiet:
            log(f"Discord status SET (with name override): {DISPLAY_NAME} | response: {resp}")
    except TypeError:
        resp = rpc.update(**kwargs)
        if not quiet:
            log(f"Discord status SET: {DISPLAY_NAME} | response: {resp}")
            log("(Old pypresence without name override; rebuild with: pip install -U pypresence)")


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
    prev_place = "init"
    prev_universe = None
    last_err = None

    rpc = None
    last_connect_try = 0.0
    discord_down_logged = False
    applied = False          # is our status currently shown on Discord?
    last_apply = 0.0
    active = False           # are we in the target game right now?
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
            place, joined_at = None, None
            if running:
                path, place, joined_at = detect_current_place(client_start)
                if path != prev_path:
                    if path:
                        log(f"Reading client log: {path}")
                    prev_path = path

            if place != prev_place:
                log(f"Detected join: place {place}" if place else "Not currently in a game.")
                prev_place = place

            universe, in_target = None, False
            if place:
                if place == common.PLACE_ID:
                    universe, in_target = common.UNIVERSE_ID, True
                else:
                    universe = place_to_universe(place)
                    in_target = universe is not None and universe == common.UNIVERSE_ID

            if place and universe != prev_universe:
                if universe is None:
                    log("Could not determine which game that place belongs to; will retry.")
                elif not in_target:
                    log(f"That's universe {universe}, not the target ({common.UNIVERSE_ID}); ignoring it.")
                prev_universe = universe if universe is not None else prev_universe
            if not place:
                prev_universe = None

            if in_target and not active:
                log(f"Target game detected (universe {common.UNIVERSE_ID}).")
                if joined_at and 0 <= now - joined_at < 86400:
                    start_time = joined_at
                    log(f"Using join time from the log: {time.strftime('%H:%M:%S', time.localtime(joined_at))} "
                        f"({int(now - joined_at)}s ago)")
                else:
                    start_time = int(now)
                    log("Couldn't read a usable join time from the log; using the current time.")
                active = True
            elif not in_target and active:
                active = False
                log("Left the target game.")

            # ---- keep Discord in sync (never fatal) ----
            if rpc is None and now - last_connect_try >= DISCORD_RETRY_SECONDS:
                last_connect_try = now
                rpc = try_connect()
                if rpc is not None:
                    log("Connected to Discord.")
                    discord_down_logged = False
                    applied = False
                elif not discord_down_logged:
                    log("Discord isn't available; will keep retrying in the background.")
                    discord_down_logged = True

            if rpc is not None:
                try:
                    if active and (not applied or now - last_apply >= KEEPALIVE_SECONDS):
                        set_status(rpc, start_time, quiet=applied)
                        applied, last_apply = True, now
                    elif not active and applied:
                        rpc.clear()
                        applied = False
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
