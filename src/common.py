"""Helpers shared by the app, installer and uninstaller. Most of it is Windows-only (the Windows
installer and uninstaller use it); the app's own platform layer is system.py."""

import base64
import ctypes
import io
import ntpath
import os
import subprocess
import sys
import time

GAME_NAME = "Nullscape"               # the only place either name is spelled out
APP_NAME = GAME_NAME + "DiscordRPC"   # program, folder, exe and log names
APP_EXE = APP_NAME + ".exe"
UNINSTALL_EXE = "uninstall.exe"
APP_VERSION = "1.2.1"
PUBLISHER = "Kamppix"
REPO_URL = f"https://github.com/{PUBLISHER}/{APP_NAME}"
SHORTCUT_NAME = GAME_NAME + ".lnk"   # shortcuts are named after the game, not the program

# Linux: lowercase, hyphenated command/folder/file names, and a reverse-DNS ID for the desktop
# entry and the icon (as the freedesktop.org specs recommend)
LINUX_NAME = GAME_NAME.lower() + "-discord-rpc"
APP_ID = f"io.github.{PUBLISHER.lower()}.{APP_NAME}"

PLACE_ID = 129279692364812
UNIVERSE_ID = 7623259179

UNINSTALL_REG_KEY = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" + APP_NAME

MUTEX_NAME = "Local\\" + APP_NAME + "_Instance"
EVENT_NAME = "Local\\" + APP_NAME + "_TakeOver"
HANDOFF_EVENT_NAME = "Local\\" + APP_NAME + "_ElevatedWindowShown"   # see wait_for_handoff()

CREATE_NO_WINDOW = 0x08000000

CSIDL_PROGRAMS = 0x0002
CSIDL_DESKTOPDIRECTORY = 0x0010
CSIDL_COMMON_PROGRAMS = 0x0017
CSIDL_COMMON_DESKTOPDIRECTORY = 0x0019

LOG_ARG = "--log"
LOG_MAX_BYTES = 1_000_000   # a bigger log is moved to <name>.log.old on the next start

_log_path = None   # set by start_log() when started with --log


def start_log(source_name):
    """Set up log() for this program, at its start: with --log, the log goes to a file in the
    working directory, named after the program (e.g. NullscapeDiscordRPC.log for
    NullscapeDiscordRPC.exe; source_name when run from source). It's also printed to the console,
    if there is one: the Windows exes are windowed programs and have none."""
    global _log_path
    if LOG_ARG in sys.argv:
        _log_path = os.path.join(os.getcwd(), log_name(source_name))   # absolute: stays put if the CWD changes
        try:
            if os.path.getsize(_log_path) > LOG_MAX_BYTES:
                os.replace(_log_path, _log_path + ".old")
        except OSError:
            pass


def program_file():
    """This program's file name: the exe or AppImage (or the script, when run from source)."""
    built = os.environ.get("APPIMAGE") or (sys.executable if getattr(sys, "frozen", False) else "")
    return os.path.basename(built or sys.argv[0])


def log_name(source_name):
    """The --log file's name: the program's, e.g. NullscapeDiscordRPC.log for NullscapeDiscordRPC.exe
    (source_name + ".log" when run from source)."""
    if os.environ.get("APPIMAGE") or getattr(sys, "frozen", False):
        return os.path.splitext(program_file())[0] + ".log"
    return source_name + ".log"


# ------------------------------ Command line ------------------------------

HELP_ARGS = ("-h", "--help")


def usage_text(description, options=(), notes=()):
    """--help text (Linux: the Windows exes have no console to print it to): options is
    [(option, explanation)]. --log and --help are added."""
    import textwrap
    program = program_file()
    options = list(options) + [
        (LOG_ARG, "also write the log to a log file in the current folder"),
        ("-h, --help", "show this help, then exit")]
    lines = [f"{APP_NAME} {APP_VERSION}", *textwrap.wrap(description, 92), "", f"Usage: {program} [option]", ""]
    width = max(len(name) for name, _ in options)
    for name, text in options:
        wrapped = textwrap.wrap(text, 92 - width) or [""]
        lines.append(f"  {name.ljust(width)}  {wrapped[0]}")
        lines += [" " * (width + 4) + more for more in wrapped[1:]]
    return "\n".join(lines + list(notes))


def handle_args(usage, options=(), with_value=()):
    """Check the command line: with -h/--help//? show the help and exit; with an unknown option,
    show it with the help and exit with code 2. usage() returns the help text; options are the
    program's own (besides --log and the help ones), with_value the ones followed by a value."""
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] in with_value:
            i += 2
            continue
        if args[i] not in options and args[i] not in HELP_ARGS and args[i] != LOG_ARG:
            print(f"Unknown option: {args[i]}\n\n{usage()}")
            sys.exit(2)
        i += 1
    if any(arg in HELP_ARGS for arg in args):
        print(usage())
        sys.exit(0)


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    if _log_path:
        try:
            with open(_log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass   # e.g. folder not writable: never let logging break the program
    if sys.stdout is not None:   # None: no console
        try:
            print(line, flush=True)
        except Exception:
            pass


# ------------------------------ Paths ------------------------------

def resource_path(name):
    """A file bundled into the exe at build time (or next to this script when run from source)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def default_install_dir():
    """Per user (%LOCALAPPDATA%\\Programs, where Windows keeps per-user apps): needs no admin rights."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    return ntpath.join(base, "Programs", APP_NAME)


def all_users_install_dir():
    """For all users (Program Files): needs admin rights."""
    return ntpath.join(os.environ.get("ProgramFiles") or r"C:\Program Files", APP_NAME)


def can_write(path):
    """Can this process create files in path? If it doesn't exist yet: in the nearest existing
    folder above it, where it would be created."""
    folder = path
    while not os.path.isdir(folder):
        parent = os.path.dirname(folder)
        if parent == folder:
            return False
        folder = parent
    try:
        import tempfile
        fd, probe = tempfile.mkstemp(dir=folder, prefix=".write-test-")
        os.close(fd)
        os.remove(probe)
        return True
    except OSError:
        return False


def normalize_install_dir(path):
    """The install directory as a clean absolute path (the folder itself is the user's choice)."""
    p = os.path.expandvars((path or "").strip().strip('"'))
    if not p:
        p = default_install_dir()
    if len(p) == 2 and p[1] == ":":
        p += "\\"   # "D:" is the drive (not Windows' "current folder on D:")
    p = ntpath.abspath(p)
    if len(p) > 3:
        p = p.rstrip("\\/")
    return p


def is_valid_install_dir(path):
    """An installation of ours: our exe and uninstaller are inside. (The folder may also hold
    other things, e.g. a drive root: only our own files are ever removed, see program_files().)"""
    return (
        bool(path)
        and os.path.isfile(os.path.join(path, APP_EXE))
        and os.path.isfile(os.path.join(path, UNINSTALL_EXE))
    )


# ------------------------------ Program files ------------------------------
# Uninstalling (and moving an installation elsewhere) removes only the installation's own files,
# then the folders that became empty, the installation folder included. Never anything else, and
# never a folder with anything left in it.

PROGRAM_SUBFOLDER = "_internal"   # the app's libraries (PyInstaller)
PENDING_EXE = APP_EXE + ".new"    # the app's own exe with an updated icon, until it's swapped in
STAGING_PREFIX = f".{APP_NAME}-removing"   # see set_aside()
# The --log files of the app and the uninstaller, when started in their folder (see start_log())
APP_LOGS = [APP_NAME + ".log", APP_NAME + ".log.old"]
UNINSTALL_LOGS = [os.path.splitext(UNINSTALL_EXE)[0] + ".log", os.path.splitext(UNINSTALL_EXE)[0] + ".log.old"]


def program_files(install_dir):
    """The program files of the installation that exist, as paths relative to its folder: the
    exes, icon.ico, a pending exe, the log files, everything in _internal, and the folder's
    shortcut if it points at this installation. (The installer refuses a folder where another
    program already has files by these names, see installer.name_clashes().)"""
    files = [name for name in [APP_EXE, UNINSTALL_EXE, "icon.ico", PENDING_EXE] + APP_LOGS + UNINSTALL_LOGS
             if os.path.isfile(os.path.join(install_dir, name))]
    for root, _, names in os.walk(os.path.join(install_dir, PROGRAM_SUBFOLDER)):
        files += [os.path.relpath(os.path.join(root, n), install_dir) for n in names]
    shortcut = os.path.join(install_dir, SHORTCUT_NAME)
    if os.path.isfile(shortcut) and shortcut_is_ours(shortcut, read_shortcut_targets([shortcut]), install_dir):
        files.append(SHORTCUT_NAME)
    return files


def leftovers(install_dir, later=()):
    """What's still in an installation folder besides its program files (and the ones in `later`,
    deleted once the uninstaller has exited): things that aren't ours, which are left alone, and so
    the folder stays."""
    skip = {n.lower() for n in later}
    try:
        return sorted(n for n in os.listdir(install_dir)
                      if n.lower() not in skip and not n.startswith(STAGING_PREFIX))
    except OSError:
        return []


def leftovers_warning(install_dir):
    """The warning shown for leftovers()."""
    return (f"Some files were left in {install_dir}, because they aren't part of {APP_NAME}")


class SetAsideError(Exception):
    """A program file couldn't be moved aside (see set_aside()): .name, .error."""

    def __init__(self, name, error):
        super().__init__(f"{name}: {error}")
        self.name, self.error = name, error


def retry(action, tries=6):
    """Run action(), retrying briefly: files can stay locked for a moment after a program exits."""
    for i in range(tries):
        try:
            return action()
        except FileNotFoundError:
            return None
        except OSError:
            if i == tries - 1:
                raise
            time.sleep(0.5)


def set_aside(install_dir, rels):
    """Move these program files into a new staging folder inside the installation folder, one
    rename each. A rename fails just like a delete would (a file in use, access denied): then
    everything is moved back and SetAsideError is raised, and nothing at all has changed.
    Returns (staging folder, the files moved), for finish_removal() or put_back()."""
    staging = os.path.join(install_dir, STAGING_PREFIX)
    n = 1
    while os.path.exists(staging):
        n += 1
        staging = os.path.join(install_dir, f"{STAGING_PREFIX}{n}")
    os.mkdir(staging)
    moved = []
    for rel in rels:
        src, dst = os.path.join(install_dir, rel), os.path.join(staging, rel)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            retry(lambda: os.rename(src, dst))
        except OSError as e:
            log(f"Couldn't move {src} aside: {e}; moving everything back.")
            put_back(install_dir, staging, moved)
            raise SetAsideError(rel, e) from e
        moved.append(rel)
    return staging, moved


def _folders_of(base, rels):
    """The folders between base and these files (base itself not included), deepest first."""
    folders = set()
    for rel in rels:
        parent = os.path.dirname(rel)
        while parent:
            folders.add(os.path.join(base, parent))
            parent = os.path.dirname(parent)
    return sorted(folders, key=lambda f: f.count(os.sep), reverse=True)


def _remove_empty(folders):
    for folder in folders:
        try:
            os.rmdir(folder)   # only ever succeeds on an empty folder
        except OSError:
            pass


def remove_empty_folders(base, rels, folder_too=False):
    """Remove the folders these files were in (relative to base) if they're empty now, and base
    itself too if asked (again only if empty)."""
    _remove_empty(_folders_of(base, rels) + ([base] if folder_too else []))


def put_back(install_dir, staging, moved):
    """Undo set_aside()."""
    for rel in reversed(moved):
        try:
            os.makedirs(os.path.dirname(os.path.join(install_dir, rel)), exist_ok=True)
            os.rename(os.path.join(staging, rel), os.path.join(install_dir, rel))
        except OSError as e:
            log(f"Couldn't move {rel} back: {e}")
    # (also the folders made for a file that then couldn't be moved)
    _remove_empty([root for root, _, _ in os.walk(staging, topdown=False)])


def finish_removal(install_dir, staging, moved, later=()):
    """Delete the files set aside, then the folders that became empty (the staging folder, the
    program's subfolders, and the installation folder itself, only if nothing else is left in it).
    Files in `later` (still in the installation folder, e.g. the running uninstaller) and any file
    that's still locked are deleted by a helper once this program has exited, which then removes
    the empty folders the same way."""
    left = [os.path.join(install_dir, rel) for rel in later]
    for rel in moved:
        path = os.path.join(staging, rel)
        try:
            retry(lambda: os.remove(path))
        except PermissionError:
            try:
                os.chmod(path, 0o666)   # read-only
                os.remove(path)
            except OSError:
                left.append(path)
        except OSError:
            left.append(path)
    folders = (_folders_of(staging, moved) + [staging]
               + _folders_of(install_dir, list(moved) + list(later)) + [install_dir])
    _remove_empty(folders)   # the ones that are empty already
    if left:
        log("Deleting once this program exits: " + ", ".join(left))
        remove_after_exit(left, [f for f in folders if os.path.isdir(f)])


def known_folder(csidl):
    """A shell folder of the account this process runs as."""
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
        return buf.value
    return None


def _session_id(pid):
    sid = ctypes.c_ulong()
    return sid.value if ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid)) else None


def interactive_user():
    """The person at the keyboard (owner of this session's explorer.exe, as DOMAIN\\name), even if
    a different administrator approved the UAC prompt and we now run as them."""
    try:
        import psutil
        mine = _session_id(os.getpid())
        users = [(p.info["pid"], p.info["username"]) for p in psutil.process_iter(["pid", "name", "username"])
                 if (p.info["name"] or "").lower() == "explorer.exe" and p.info["username"]]
        for pid, user in users:
            if _session_id(pid) == mine:
                return user
        if users:
            return users[0][1]
    except Exception:
        pass
    return current_user()


def current_user():
    """The account this process runs as (DOMAIN\\name)."""
    dom, user = os.environ.get("USERDOMAIN"), os.environ.get("USERNAME")
    return f"{dom}\\{user}" if dom and user else user


# Per-user shell folders, as named under ...\Explorer\User Shell Folders in each user's registry
_USER_SHELL_FOLDERS = {CSIDL_DESKTOPDIRECTORY: "Desktop", CSIDL_PROGRAMS: "Programs"}


def _folder_of_other_user(user, csidl):
    """A per-user shell folder of another account, read from that user's (loaded) registry."""
    import winreg
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    sid_buf, sid_size = ctypes.create_string_buffer(256), wintypes.DWORD(256)
    dom_buf, dom_size, use = ctypes.create_unicode_buffer(256), wintypes.DWORD(256), wintypes.DWORD()
    if not advapi.LookupAccountNameW(None, user, sid_buf, ctypes.byref(sid_size),
                                     dom_buf, ctypes.byref(dom_size), ctypes.byref(use)):
        return None
    sid_str = ctypes.c_wchar_p()
    if not advapi.ConvertSidToStringSidW(sid_buf, ctypes.byref(sid_str)):
        return None
    sid = sid_str.value
    ctypes.windll.kernel32.LocalFree(sid_str)

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        rf"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\{sid}") as k:
        profile = os.path.expandvars(winreg.QueryValueEx(k, "ProfileImagePath")[0])
    with winreg.OpenKey(winreg.HKEY_USERS,
                        rf"{sid}\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
        raw = winreg.QueryValueEx(k, _USER_SHELL_FOLDERS[csidl])[0]
    # Expand that user's variables (not ours: we may be running as someone else)
    for var, value in (("%USERPROFILE%", profile),
                       ("%APPDATA%", os.path.join(profile, "AppData", "Roaming")),
                       ("%LOCALAPPDATA%", os.path.join(profile, "AppData", "Local"))):
        raw = raw.replace(var, value).replace(var.lower(), value)
    return os.path.expandvars(raw)


def user_folder(csidl):
    """A per-user shell folder (desktop, Start Menu) of the person at the keyboard. Normally the
    same as known_folder(), but when a different administrator approved the UAC prompt the
    elevated installer runs as them: their desktop isn't the one that should get the shortcut."""
    user = interactive_user()
    if user and current_user() and user.lower() != current_user().lower():
        try:
            folder = _folder_of_other_user(user, csidl)
            if folder:
                return folder
        except Exception:
            pass
    return known_folder(csidl)


def shortcut_locations():
    """Every place outside the installation folder where the installer may have put a shortcut:
    [(label, full_path)]. (The one inside the folder is simply deleted along with it.) The desktop
    and Start Menu ones are the logged-on user's, also when we run as another administrator."""
    spots = [
        ("desktop", user_folder(CSIDL_DESKTOPDIRECTORY)),
        ("desktop (all users)", known_folder(CSIDL_COMMON_DESKTOPDIRECTORY)),
        ("Start Menu", user_folder(CSIDL_PROGRAMS)),
        ("Start Menu (all users)", known_folder(CSIDL_COMMON_PROGRAMS)),
    ]
    return [(label, os.path.join(folder, SHORTCUT_NAME)) for label, folder in spots if folder]


# ------------------------------ Windows helpers ------------------------------

def same_path(a, b):
    return bool(a and b) and ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b))


def registered_location(hive):
    """InstallLocation of the "installed apps" entry in this registry hive (HKEY_LOCAL_MACHINE: for
    the whole machine, HKEY_CURRENT_USER: just this user), or None if there's no entry."""
    import winreg
    try:
        with winreg.OpenKey(hive, UNINSTALL_REG_KEY) as k:
            return winreg.QueryValueEx(k, "InstallLocation")[0] or ""
    except OSError:
        return None


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return True   # no UAC to ask through


def relaunch_elevated(extra_args=()):
    """Start this program again with administrator rights (Windows shows the UAC prompt), with its
    current arguments plus extra_args. True if it started, False if the prompt was declined."""
    args = sys.argv[1:] + list(extra_args)
    if not getattr(sys, "frozen", False):
        args = [os.path.abspath(sys.argv[0])] + args
    # (in the same working directory: without one it would be System32, and --log writes there)
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable,
                                               subprocess.list2cmdline(args), os.getcwd(), 1) > 32


def prepare_handoff():
    """Before relaunch_elevated(): create the event the elevated copy signals once its window is
    on screen (see wait_for_handoff())."""
    return _k32().CreateEventW(None, True, False, HANDOFF_EVENT_NAME)


def wait_for_handoff(root, event, timeout=20.0):
    """Keep this window up until the elevated copy shows its own (it takes a moment to start), so
    the installer doesn't seem to vanish; then close it. Never longer than timeout seconds."""
    deadline = time.monotonic() + timeout
    root.config(cursor="watch")

    def check():
        if _k32().WaitForSingleObject(event, 0) == WAIT_OBJECT_0 or time.monotonic() > deadline:
            close_handle(event)
            root.destroy()
        else:
            root.after(100, check)
    check()


def signal_handoff():
    """In the elevated copy, once its window is shown: let the original window close."""
    event = _k32().OpenEventW(EVENT_MODIFY_STATE, False, HANDOFF_EVENT_NAME)
    if event:
        _k32().SetEvent(event)
        close_handle(event)


_ui_scale = 1.0   # display scaling (1.0 = 100 %, 2.0 = 200 % on a 4K screen), see px()


def set_dpi_aware():
    """Draw the windows sharp at any display scaling (instead of Windows stretching them blurry),
    and remember the scaling for px()."""
    global _ui_scale
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # system DPI aware
    except Exception:
        pass
    try:
        _ui_scale = ctypes.windll.user32.GetDpiForSystem() / 96
    except Exception:
        try:
            dc = ctypes.windll.user32.GetDC(None)
            _ui_scale = ctypes.windll.gdi32.GetDeviceCaps(dc, 88) / 96   # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(None, dc)
        except Exception:
            pass


def px(*sizes):
    """Sizes given in pixels at 100 % display scaling, in real pixels for this display (fonts
    scale by themselves, pixel sizes don't). One size gives a number, several a tuple."""
    scaled = tuple(round(size * _ui_scale) for size in sizes)
    return scaled[0] if len(scaled) == 1 else scaled


def _q(s):
    return s.replace("'", "''")


def run_powershell(script, timeout=30):
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", encoded],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW, timeout=timeout,
    )


def create_shortcut(lnk_path, target, workdir, icon_path=None):
    os.makedirs(os.path.dirname(lnk_path), exist_ok=True)
    icon = f"{icon_path},0" if icon_path else f"{target},0"
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
        "$s.TargetPath = '%s';"
        "$s.WorkingDirectory = '%s';"
        "$s.IconLocation = '%s';"
        "$s.Description = '%s';"
        "$s.Save()"
    ) % (_q(lnk_path), _q(target), _q(workdir), _q(icon), GAME_NAME)
    r = run_powershell(script)
    if r.returncode != 0 or not os.path.exists(lnk_path):
        raise RuntimeError(r.stderr.decode(errors="ignore").strip() or "PowerShell failed")


def read_shortcut_targets(paths):
    """{shortcut path: target path}. Empty dict if it couldn't be read."""
    if not paths:
        return {}
    items = ",".join("'%s'" % _q(p) for p in paths)
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
        "$ws = New-Object -ComObject WScript.Shell;"
        "foreach ($p in @(%s)) { try { Write-Output ($p + '|' + $ws.CreateShortcut($p).TargetPath) }"
        " catch { Write-Output ($p + '|') } }"
    ) % items
    out = {}
    try:
        r = run_powershell(script)
        for line in r.stdout.decode("utf-8-sig", errors="ignore").splitlines():
            if "|" in line:
                p, t = line.rsplit("|", 1)
                out[p.strip()] = t.strip()
    except Exception:
        pass
    return out


def shortcut_is_ours(path, targets, install_dir):
    t = targets.get(path)
    if not t:
        return True  # couldn't resolve the target: fall back to matching by name
    mine = ntpath.normcase(ntpath.normpath(ntpath.join(install_dir, APP_EXE)))
    return ntpath.normcase(ntpath.normpath(t)) == mine


def installed_shortcuts(install_dir):
    """Every shortcut of ours that actually exists (and points at this installation): [(label, path)]."""
    existing = [(label, p) for label, p in shortcut_locations() if os.path.isfile(p)]
    targets = read_shortcut_targets([p for _, p in existing])
    return [(label, p) for label, p in existing if shortcut_is_ours(p, targets, install_dir)]


def installations():
    """The folders of the installations listed in "installed apps" (at most one for this user,
    one for the whole machine) that are still there."""
    import winreg
    found = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        location = registered_location(hive)
        if is_valid_install_dir(location) and not any(same_path(location, f) for f in found):
            found.append(location)
    return found


def removal_needs_admin(install_dir, shortcuts):
    """Does removing this installation need administrator rights? Only if it's in a folder we
    can't write to (e.g. Program Files), registered for the whole machine, or has shortcuts for
    all users."""
    import winreg
    machine_wide = same_path(registered_location(winreg.HKEY_LOCAL_MACHINE), install_dir)
    return (not can_write(install_dir) or machine_wide
            or any("all users" in label for label, _ in shortcuts))


def delete_registry_entries(install_dir):
    """Remove the "installed apps" entry of this installation (machine-wide or per-user), leaving
    alone an entry that belongs to another installation of ours in a different folder."""
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        location = registered_location(hive)
        if location is not None and (location == "" or same_path(location, install_dir)):
            try:
                winreg.DeleteKey(hive, UNINSTALL_REG_KEY)
            except OSError:
                pass


class PlainError(RuntimeError):
    """An error whose message is already written for the user: friendly_error() shows it as is."""


def friendly_error(error):
    """A short, plain reason for an error shown in the installer/uninstaller: never raw technical
    output (e.g. PowerShell's error dumps)."""
    text = str(error)
    if isinstance(error, PlainError):
        return text
    lowered = text.lower()
    # (before "access denied": Windows reports a locked file as a permission error too)
    if "being used by another process" in lowered or "[winerror 32]" in lowered:
        return "in use by another program"
    if (isinstance(error, PermissionError) or "unauthorizedaccess" in lowered
            or "access is denied" in lowered or "access denied" in lowered or "[winerror 5]" in lowered):
        return "access denied"
    if "folder not found" in lowered:
        return "folder not found"
    if isinstance(error, FileNotFoundError):
        return "file not found"
    if isinstance(error, RuntimeError) and text and "\n" not in text and "<" not in text:
        return text   # our own messages are already plain
    return "unexpected error"


def system_uses_dark_theme():
    """Windows' "app mode" setting (Settings > Personalization > Colors)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def apply_theme(root):
    """Give the installer/uninstaller window the system's light or dark look: the Sun Valley ttk
    theme (Windows 11 style) in the matching mode. Falls back to the classic light "vista" theme.
    Create the window hidden and show it with show_window(), which also sets the title bar to match.
    Returns colours for text the theme doesn't style itself."""
    from tkinter import ttk, TclError
    dark = system_uses_dark_theme()
    try:
        import sv_ttk
        sv_ttk.set_theme("dark" if dark else "light", root)
        # The theme sets its colours and fonts when the main window gets <<ThemeChanged>>, which a
        # hidden main window (an error dialog on its own) never does: do it directly
        root.tk.call("configure_colors")
        _scale_theme(root)
    except Exception:
        dark = False   # the classic theme only comes in light
        try:
            ttk.Style(root).theme_use("vista")
        except TclError:
            pass
    root._theme_dark = dark   # for show_window() and dialog()
    return {"warning": "#f0b060" if dark else "#9a5b00"}


def _scale_theme(root):
    """The Sun Valley theme is made for 100 % display scaling: its fonts are sized in pixels
    (negative sizes), so scale them by px(). (Its controls are images that stretch to fit the
    text; only the checkbox squares keep their size.)"""
    if _ui_scale == 1.0 or getattr(root, "_theme_scaled", False):
        return
    root._theme_scaled = True
    tk = root.tk
    for name in tk.splitlist(tk.call("font", "names")):
        if str(name).startswith("SunValley"):
            size = int(tk.call("font", "configure", name, "-size"))
            if size < 0:
                tk.call("font", "configure", name, "-size", -px(-size))


def show_window(window):
    """Show a window that was created hidden, with its title bar already in the theme's light or
    dark mode, so it never flashes the wrong colour or needs redrawing."""
    window.update_idletasks()   # the window exists (hidden) from here on
    _set_title_bar_mode(window, getattr(window, "_theme_dark", False))
    window.deiconify()
    window.lift()
    window.focus_force()


def dialog(parent, title, text, buttons=("OK",), default=0):
    """A small modal message window in the app's theme, used instead of Windows' own message boxes
    (those ignore dark mode). Returns the index of the button clicked, or None if it was closed."""
    import tkinter as tk
    from tkinter import ttk
    win = tk.Toplevel(parent)
    win.withdraw()
    win.title(title)
    win.resizable(False, False)
    shown_over_parent = bool(parent.winfo_viewable())
    if shown_over_parent:
        win.transient(parent)
    result = [None]

    def choose(index):
        result[0] = index
        win.destroy()

    frame = ttk.Frame(win, padding=px(22, 20))
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=text, wraplength=px(420), justify="left").pack(anchor="w")
    row = ttk.Frame(frame)
    row.pack(fill="x", pady=px(20, 0))
    accent = ttk.Style(win).theme_use().startswith("sun-valley")   # the classic theme has no accent style
    for index, label in reversed(list(enumerate(buttons))):
        button = ttk.Button(row, text=label, command=lambda i=index: choose(i),
                            style="Accent.TButton" if accent and index == default else "TButton")
        button.pack(side="right", padx=px(8, 0))
        if index == default:
            button.focus_set()
    win.bind("<Return>", lambda e: choose(default))
    win.bind("<Escape>", lambda e: choose(0 if len(buttons) == 1 else None))
    win.protocol("WM_DELETE_WINDOW", lambda: choose(0 if len(buttons) == 1 else None))

    win.update_idletasks()
    width, height = win.winfo_reqwidth(), win.winfo_reqheight()
    if shown_over_parent:
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 3
    else:
        x = (win.winfo_screenwidth() - width) // 2
        y = (win.winfo_screenheight() - height) // 3
    win.geometry(f"+{x}+{y}")
    win._theme_dark = getattr(parent, "_theme_dark", False)
    show_window(win)
    win.grab_set()
    parent.wait_window(win)
    return result[0]


def size_window(root, width, pages, min_height=0):
    """Fix the window at this width and the height of its tallest page (themes differ in how much
    room their controls need), centred a bit above the middle of the screen. Sizes are as for
    px()."""
    root.update_idletasks()
    width = px(width)
    height = max([px(min_height)] + [page.winfo_reqheight() for page in pages])
    x = (root.winfo_screenwidth() - width) // 2
    y = (root.winfo_screenheight() - height) // 3
    root.geometry(f"{width}x{height}+{x}+{y}")


def _set_title_bar_mode(window, dark):
    """Light or dark title bar for a (not yet shown) window."""
    try:
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (older Windows 10 builds: 19)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value),
                                                          ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


def set_window_icon(root):
    """Use the game icon (icon.png, bundled at build time) for a Tk window and the ones it opens."""
    png = resource_path("icon.png")
    if not os.path.isfile(png):
        return
    try:
        import tkinter as tk   # imported here: the app itself is built without tkinter
        with open(png, "rb") as f:
            png_bytes = f.read()
        # Windows scales a single large icon down very crudely (pixelated), so hand Tk properly
        # resampled copies at the usual icon sizes and let it pick the closest ones.
        images = []
        try:
            square = _square_rgba(png_bytes)
            for side in (256, 128, 64, 48, 40, 32, 24, 20, 16):
                buf = io.BytesIO()
                square.resize((side, side), _lanczos()).save(buf, format="PNG")
                images.append(tk.PhotoImage(master=root, data=base64.b64encode(buf.getvalue())))
        except Exception:
            images = [tk.PhotoImage(master=root, data=base64.b64encode(png_bytes))]
        root._icon_images = images   # keep references alive, or Tk drops the icon
        root.iconphoto(True, *images)
    except Exception:
        pass


def own_pids():
    """This process, plus the PyInstaller onefile launcher that started it (same exe; it keeps the
    exe locked until we exit)."""
    import psutil
    pids = [os.getpid()]
    try:
        parent = psutil.Process().parent()
        if parent and ntpath.normcase(parent.exe()) == ntpath.normcase(sys.executable):
            pids.append(parent.pid)
    except psutil.Error:
        pass
    return pids


def run_after_exit(script):
    """Run a PowerShell script in a hidden helper once this program's processes have exited,
    e.g. to delete or replace our own (locked while running) exe."""
    wait = "foreach ($p in @(%s)) { Wait-Process -Id $p -ErrorAction SilentlyContinue };" % (
        ",".join(str(p) for p in own_pids()))
    encoded = base64.b64encode((wait + script).encode("utf-16le")).decode("ascii")
    windir = os.environ.get("SystemRoot", "C:\\Windows")
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=windir, close_fds=True,
        # NEW_PROCESS_GROUP | NO_WINDOW. Not DETACHED_PROCESS: PowerShell needs a (hidden) console
        # and silently never starts without one.
        creationflags=0x00000200 | CREATE_NO_WINDOW,
    )


def remove_after_exit(files, folders=()):
    """Once this program has exited: delete these files, then these folders, each only if it's
    empty by then (never anything inside them). Retries for a while if a file stays locked."""
    listed = lambda paths: ",".join("'%s'" % _q(p) for p in paths)
    run_after_exit(
        "$files = @(%s); $folders = @(%s);"
        "for ($i = 0; $i -lt 60; $i++) {"
        " $left = $false;"
        " foreach ($f in $files) { if (Test-Path -LiteralPath $f -PathType Leaf) {"
        "  Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue;"
        "  if (Test-Path -LiteralPath $f -PathType Leaf) { $left = $true } } };"
        " if (-not $left) { break };"
        " Start-Sleep -Milliseconds 500"
        "};"
        # Directory.Delete(path, false) refuses a folder that isn't empty
        "foreach ($d in $folders) { try { [System.IO.Directory]::Delete($d, $false) } catch {} }"
        % (listed(files), listed(folders)))


def replace_after_exit(src, dst):
    """Move src over dst once this program has exited (gives up after ~30s if dst stays locked,
    e.g. because a new instance already started; leftovers are retried on the next launch)."""
    run_after_exit(
        "$s = '%s'; $d = '%s';"
        "for ($i = 0; $i -lt 60; $i++) {"
        " try { Move-Item -LiteralPath $s -Destination $d -Force -ErrorAction Stop; break }"
        " catch { Start-Sleep -Milliseconds 500 }"
        "}" % (_q(src), _q(dst)))


def notify_icon_change():
    """Ask Explorer to drop cached icons so shortcuts pick up a changed .ico."""
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED
        log("Asked Windows to refresh cached icons.")
    except Exception as e:
        log(f"Could not refresh the icon cache: {e}")


# ------------------------------ Game info / icon ------------------------------

_info_cache = {}   # universe_id -> (name, icon_url)


def api_get(url, params=None):
    import requests
    r = requests.get(url, params=params, timeout=8)
    r.raise_for_status()
    return r.json()


def fetch_game_info(universe_id):
    """Return (name, icon_url); cached once both were fetched successfully."""
    if universe_id in _info_cache:
        return _info_cache[universe_id]

    name, icon = None, None
    try:
        data = api_get("https://games.roblox.com/v1/games",
                       {"universeIds": universe_id}).get("data", [])
        if data:
            name = data[0]["name"]
            log(f"Fetched game name: {name}")
        else:
            log("Game info request worked but returned no game for that universe ID.")
    except Exception as e:
        log(f"Failed to fetch game name: {e}")

    try:
        data = api_get("https://thumbnails.roblox.com/v1/games/icons", {
            "universeIds": universe_id,
            "size": "512x512",
            "format": "Png",
            "isCircular": "false",
        }).get("data", [])
        if data and data[0].get("imageUrl"):
            icon = data[0]["imageUrl"]
            log(f"Fetched game icon URL: {icon}")
        else:
            log(f"Icon request returned no image (state: {data[0].get('state') if data else 'n/a'}).")
    except Exception as e:
        log(f"Failed to fetch game icon: {e}")

    if name and icon:
        _info_cache[universe_id] = (name, icon)
    return name, icon


def download_icon_png():
    """Current game icon as PNG bytes, or None."""
    import requests
    _, url = fetch_game_info(UNIVERSE_ID)
    if not url:
        return None
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    if not r.content.startswith(b"\x89PNG"):
        log("Downloaded icon isn't a PNG; ignoring it.")
        return None
    log(f"Downloaded icon ({len(r.content)} bytes).")
    return r.content


def _lanczos():
    from PIL import Image
    return Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS


def _square_rgba(png_bytes):
    """The image centered on a transparent square canvas (at least 256x256)."""
    from PIL import Image
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    side = max(img.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    if side < 256:
        square = square.resize((256, 256), _lanczos())
    return square


def png_bytes_to_ico(png_bytes):
    """ICO file contents (several sizes) made from PNG bytes."""
    square = _square_rgba(png_bytes)
    out = io.BytesIO()
    square.save(out, format="ICO",
                sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)])
    return out.getvalue()


def refresh_icon(dest_dir):
    """Download the icon and (re)build icon.ico in dest_dir. Returns (ico_path_or_None, changed).
    The PNG only ever lives in memory; icon.ico is the only file written."""
    ico_path = os.path.join(dest_dir, "icon.ico")
    try:
        os.remove(os.path.join(dest_dir, "icon.png"))   # left behind by older versions
    except OSError:
        pass

    png = None
    try:
        png = download_icon_png()
    except Exception as e:
        log(f"Icon download failed: {e}")

    if png is None:
        if os.path.exists(ico_path):
            log("Keeping the existing icon.")
            return ico_path, False
        return None, False

    try:
        new = png_bytes_to_ico(png)
    except Exception as e:
        log(f"Icon conversion failed: {e}")
        return (ico_path if os.path.exists(ico_path) else None), False

    old = None
    if os.path.exists(ico_path):
        with open(ico_path, "rb") as f:
            old = f.read()
    if new == old:   # the conversion is deterministic, so an unchanged icon gives identical bytes
        log("Icon is already up to date.")
        return ico_path, False

    try:
        with open(ico_path + ".tmp", "wb") as f:
            f.write(new)
        os.replace(ico_path + ".tmp", ico_path)
    except OSError as e:
        log(f"Could not write the icon: {e}")
        return (ico_path if os.path.exists(ico_path) else None), False

    log(f"Icon updated: {ico_path}")
    return ico_path, True


# ------------------------------ Icons inside our exes ------------------------------

RT_ICON = 3
RT_GROUP_ICON = 14


def _pe_image_end(data):
    """Where the PE image ends; PyInstaller appends its archive after this point."""
    import struct
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("not a PE file")
    n_sections, = struct.unpack_from("<H", data, pe + 6)
    opt_size, = struct.unpack_from("<H", data, pe + 20)
    table = pe + 24 + opt_size
    end = table + 40 * n_sections
    for i in range(n_sections):
        raw_size, raw_ptr = struct.unpack_from("<II", data, table + 40 * i + 16)
        if raw_size:
            end = max(end, raw_ptr + raw_size)
    return end


def _icon_resources(exe_path):
    """[(type, name, lang)] of the icon resources currently in an exe; name is an int ID or a str."""
    from ctypes import wintypes, c_void_p, WINFUNCTYPE
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.LoadLibraryExW.argtypes, k.LoadLibraryExW.restype = [wintypes.LPCWSTR, c_void_p, wintypes.DWORD], c_void_p
    k.FreeLibrary.argtypes = [c_void_p]
    names_cb = WINFUNCTYPE(wintypes.BOOL, c_void_p, c_void_p, c_void_p, c_void_p)
    langs_cb = WINFUNCTYPE(wintypes.BOOL, c_void_p, c_void_p, c_void_p, wintypes.WORD, c_void_p)
    k.EnumResourceNamesW.argtypes = [c_void_p, c_void_p, names_cb, c_void_p]
    k.EnumResourceLanguagesW.argtypes = [c_void_p, c_void_p, c_void_p, langs_cb, c_void_p]

    module = k.LoadLibraryExW(exe_path, None, 0x00000002 | 0x00000020)  # AS_DATAFILE | AS_IMAGE_RESOURCE
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    found = []
    try:
        for rtype in (RT_GROUP_ICON, RT_ICON):
            names = []

            def on_name(_m, _t, name, _p):
                names.append(name if name <= 0xFFFF else ctypes.wstring_at(name))
                return True

            k.EnumResourceNamesW(module, rtype, names_cb(on_name), None)
            for name in names:
                name_arg = name if isinstance(name, int) else ctypes.cast(ctypes.c_wchar_p(name), c_void_p)
                langs = []
                k.EnumResourceLanguagesW(module, rtype, name_arg,
                                         langs_cb(lambda _m, _t, _n, lang, _p: langs.append(lang) or True), None)
                found += [(rtype, name, lang) for lang in langs]
    finally:
        k.FreeLibrary(module)
    return found


def _write_icon_resources(exe_path, ico_bytes):
    """Replace every icon in exe_path (a copy that isn't running) with the images of an .ico."""
    import struct
    from ctypes import wintypes, c_void_p
    count, = struct.unpack_from("<H", ico_bytes, 4)
    entries = [struct.unpack_from("<BBBBHHII", ico_bytes, 6 + 16 * i) for i in range(count)]
    group = struct.pack("<HHH", 0, 1, count) + b"".join(
        struct.pack("<BBBBHHIH", w, h, colors, 0, planes, bits, size, i + 1)
        for i, (w, h, colors, _, planes, bits, size, _) in enumerate(entries))

    old = _icon_resources(exe_path)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.BeginUpdateResourceW.argtypes, k.BeginUpdateResourceW.restype = [wintypes.LPCWSTR, wintypes.BOOL], c_void_p
    k.UpdateResourceW.argtypes = [c_void_p, c_void_p, c_void_p, wintypes.WORD, c_void_p, wintypes.DWORD]
    k.EndUpdateResourceW.argtypes = [c_void_p, wintypes.BOOL]

    handle = k.BeginUpdateResourceW(exe_path, False)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    keep = []   # buffers must stay alive until EndUpdateResource

    def update(rtype, name, lang, data):
        if isinstance(name, str):
            keep.append(ctypes.create_unicode_buffer(name))
            name = ctypes.cast(keep[-1], c_void_p)
        buf = None
        if data is not None:
            keep.append(ctypes.create_string_buffer(data, len(data)))
            buf = ctypes.cast(keep[-1], c_void_p)
        if not k.UpdateResourceW(handle, rtype, name, lang, buf, len(data) if data else 0):
            raise ctypes.WinError(ctypes.get_last_error())

    try:
        for rtype, name, lang in old:
            update(rtype, name, lang, None)   # delete
        lang = next((lg for t, _, lg in old if t == RT_GROUP_ICON), 0)
        for i, (*_, size, offset) in enumerate(entries):
            update(RT_ICON, i + 1, lang, ico_bytes[offset:offset + size])
        update(RT_GROUP_ICON, 1, lang, group)
    except Exception:
        k.EndUpdateResourceW(handle, True)    # discard
        raise
    if not k.EndUpdateResourceW(handle, False):
        raise ctypes.WinError(ctypes.get_last_error())


def set_exe_icon(exe_path, ico_bytes, dest=None):
    """Write a copy of exe_path with its icon replaced to dest (default: replace exe_path itself,
    which then must not be running). The data PyInstaller appends after the PE image is kept:
    the resource API would otherwise drop it and the exe would no longer start."""
    dest = dest or exe_path
    with open(exe_path, "rb") as f:
        data = f.read()
    end = _pe_image_end(data)
    tmp = dest + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data[:end])
    try:
        _write_icon_resources(tmp, ico_bytes)
        with open(tmp, "rb") as f:
            image = f.read()
        with open(tmp, "wb") as f:
            f.write(image[:_pe_image_end(image)] + data[end:])
        os.replace(tmp, dest)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ------------------------------ Single instance / take-over ------------------------------

SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002
MUTEX_MODIFY_STATE = 0x0001
WAIT_OBJECT_0 = 0
WAIT_ABANDONED_0 = 0x80
WAIT_TIMEOUT = 0x102
ERROR_ALREADY_EXISTS = 183

_k32_cache = None


def _k32():
    global _k32_cache
    if _k32_cache is None:
        from ctypes import c_void_p, c_int, c_uint32, c_wchar_p, POINTER
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.argtypes, k.CreateMutexW.restype = [c_void_p, c_int, c_wchar_p], c_void_p
        k.OpenMutexW.argtypes, k.OpenMutexW.restype = [c_uint32, c_int, c_wchar_p], c_void_p
        k.CreateEventW.argtypes, k.CreateEventW.restype = [c_void_p, c_int, c_int, c_wchar_p], c_void_p
        k.OpenEventW.argtypes, k.OpenEventW.restype = [c_uint32, c_int, c_wchar_p], c_void_p
        k.OpenProcess.argtypes, k.OpenProcess.restype = [c_uint32, c_int, c_uint32], c_void_p
        k.SetEvent.argtypes = [c_void_p]
        k.ResetEvent.argtypes = [c_void_p]
        k.ReleaseMutex.argtypes = [c_void_p]
        k.CloseHandle.argtypes = [c_void_p]
        k.WaitForSingleObject.argtypes, k.WaitForSingleObject.restype = [c_void_p, c_uint32], c_uint32
        k.WaitForMultipleObjects.argtypes = [c_uint32, POINTER(c_void_p), c_int, c_uint32]
        k.WaitForMultipleObjects.restype = c_uint32
        _k32_cache = k
    return _k32_cache


def open_process_handle(pid):
    return _k32().OpenProcess(SYNCHRONIZE, False, pid) or None


def close_handle(handle):
    if handle:
        _k32().CloseHandle(handle)


def wait_any(handles, timeout_ms):
    """Index of the handle that got signaled, -1 on timeout, -2 if waiting failed."""
    k = _k32()
    arr = (ctypes.c_void_p * len(handles))(*handles)
    r = k.WaitForMultipleObjects(len(handles), arr, False, int(timeout_ms))
    if r == WAIT_TIMEOUT:
        return -1
    if r < len(handles):
        return r
    if WAIT_ABANDONED_0 <= r < WAIT_ABANDONED_0 + len(handles):
        return r - WAIT_ABANDONED_0
    return -2


def kill_app_processes(folder=None):
    """Force-close APP_EXE processes (only the one in `folder`, or any if None)."""
    import psutil
    me = os.getpid()
    wanted = ntpath.normcase(ntpath.join(folder, APP_EXE)) if folder else None
    victims = []
    for p in psutil.process_iter(["pid", "exe", "name"]):
        try:
            if p.info["pid"] == me:
                continue
            exe = p.info["exe"] or ""
            if wanted:
                if ntpath.normcase(exe) == wanted:
                    victims.append(p)
            elif (p.info["name"] or "").lower() == APP_EXE.lower():
                victims.append(p)
        except psutil.Error:
            pass
    for p in victims:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(victims, timeout=3)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    return len(victims)


def acquire_instance_lock(timeout=10.0):
    """
    Become THE running instance. If another one is running, ask it to exit (it clears its
    Discord status and quits cleanly) and take over; force-close it only if it doesn't react.
    Returns the take-over event handle to wait on, or None if we couldn't get the lock.
    """
    k = _k32()
    event = k.CreateEventW(None, False, False, EVENT_NAME)        # auto-reset
    mutex = k.CreateMutexW(None, True, MUTEX_NAME)                # we own it if we're first
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        log("Another instance is running; asking it to hand over.")
        k.SetEvent(event)
        r = k.WaitForSingleObject(mutex, int(timeout * 1000))
        if r not in (WAIT_OBJECT_0, WAIT_ABANDONED_0):
            log("The previous instance didn't exit in time; closing it forcefully.")
            kill_app_processes()
            r = k.WaitForSingleObject(mutex, 5000)
            if r not in (WAIT_OBJECT_0, WAIT_ABANDONED_0):
                return None
        k.ResetEvent(event)   # drop any signal nobody consumed so we don't quit ourselves
    return event


def ask_running_instance_to_exit(timeout=10.0):
    """For the installer/uninstaller. True if nothing is running (any more)."""
    k = _k32()
    mutex = k.OpenMutexW(SYNCHRONIZE | MUTEX_MODIFY_STATE, False, MUTEX_NAME)
    if not mutex:
        # "not found" = nothing is running; "access denied" = it IS running under another account
        return ctypes.get_last_error() != 5
    event = k.OpenEventW(EVENT_MODIFY_STATE, False, EVENT_NAME)
    if event:
        k.SetEvent(event)
        k.CloseHandle(event)
    r = k.WaitForSingleObject(mutex, int(timeout * 1000))
    ok = r in (WAIT_OBJECT_0, WAIT_ABANDONED_0)
    if ok:
        k.ReleaseMutex(mutex)
    k.CloseHandle(mutex)
    return ok


def stop_app(install_dir):
    """Close the app running from this folder: ask it to exit cleanly first (it clears its Discord
    status), then force-close anything still running from there, also a copy that never answered
    (e.g. one stuck while starting up)."""
    ask_running_instance_to_exit(10.0)
    kill_app_processes(install_dir)
