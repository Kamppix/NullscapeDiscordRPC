"""
What the app needs from the operating system, for Windows and Linux.

  Windows  Roblox's own client, the app's files next to its exe, a named mutex/event for the
           single instance, process handles to notice the client exiting.
  Linux    Sober (the Roblox client for Linux, a Flatpak), run as an AppImage. It stores nothing
           unless asked to integrate with the desktop (--integrate): then only a .desktop menu entry
           and its icon, which it keeps up to date. (And with --log, the log in the current folder.) The single instance is an abstract Unix socket
           (no file), pidfds notice the client exiting, and SIGTERM/SIGINT/SIGHUP exit cleanly.
"""

import glob
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time

import common

IS_WINDOWS = sys.platform == "win32"
FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))

SOBER_APP_ID = "org.vinegarhq.Sober"

if IS_WINDOWS:
    ROBLOX_PROCESS_NAMES = {"robloxplayerbeta.exe"}
    ROBLOX_LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Roblox", "logs")
    ROBLOX_LOG_GLOB = "*.log"
else:
    ROBLOX_PROCESS_NAMES = {"sober"}
    ROBLOX_LOG_DIR = os.path.expanduser(f"~/.var/app/{SOBER_APP_ID}/data/sober/sober_logs")
    ROBLOX_LOG_GLOB = "*"

# Discord's main process on either system (official, PTB, Canary and dev builds; Flatpak and Snap
# Discord run the same binaries). Compared lowercased.
DISCORD_PROCESS_NAMES = {"discord.exe", "discordptb.exe", "discordcanary.exe", "discorddevelopment.exe",
                         "discord", "discordptb", "discordcanary", "discorddevelopment"}

STOP = "stop"       # Instance.wait(): asked to exit (a newer instance, or a signal on Linux)
EXITED = "exited"   # Instance.wait(): the watched process ended (its pid is in .exited_pid)


# ------------------------------ Files ------------------------------

def newest_roblox_log(min_mtime=None):
    """The client's newest log file, ignoring ones from before min_mtime (earlier sessions)."""
    files = [f for f in glob.glob(os.path.join(ROBLOX_LOG_DIR, ROBLOX_LOG_GLOB)) if os.path.isfile(f)]
    if min_mtime is not None:
        files = [f for f in files if os.path.getmtime(f) >= min_mtime - 5]
    return max(files, key=os.path.getmtime) if files else None


# ------------------------------ Launching ------------------------------

def _host_environment():
    """Our environment as it was before the PyInstaller (and AppImage) launcher changed it, for
    starting other programs: with our bundled libraries in LD_LIBRARY_PATH, tools like xdg-open
    can fail to start whatever they hand the link to."""
    env = dict(os.environ)
    for var in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
        original = env.pop(var + "_ORIG", None)
        if original is not None:
            env[var] = original
        else:
            env.pop(var, None)
    return env


def open_url(url):
    """Open a roblox:// link with whatever handles those (Roblox on Windows, Sober on Linux)."""
    if IS_WINDOWS:
        os.startfile(url)
        return
    if not shutil.which("xdg-open"):
        raise RuntimeError("xdg-open wasn't found, so the roblox:// link can't be opened")
    subprocess.Popen(["xdg-open", url], env=_host_environment(), start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ------------------------------ Desktop integration (Linux) ------------------------------

def _data_home():
    path = os.environ.get("XDG_DATA_HOME", "")
    return path if os.path.isabs(path) else os.path.expanduser("~/.local/share")


def desktop_entry_path():
    return os.path.join(_data_home(), "applications", common.APP_ID + ".desktop")


def _icons_root():
    return os.path.join(_data_home(), "icons", "hicolor")


def launch_command():
    """What the menu entry should run, as a list of arguments: the AppImage itself (its path is in
    $APPIMAGE), or this program when it's run some other way."""
    if os.environ.get("APPIMAGE"):
        return [os.environ["APPIMAGE"]]
    return [sys.executable] if FROZEN else [sys.executable, os.path.abspath(sys.argv[0])]


def _desktop_exec(args):
    """Exec= value per the Desktop Entry spec: arguments with special characters are double-quoted
    (escaping " ` $ \\ inside), and the whole value then escapes backslashes once more."""
    def quote(arg):
        if not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
            return arg
        return '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'
    return " ".join(quote(a) for a in args).replace("\\", "\\\\")


def desktop_entry(args):
    """A freedesktop.org .desktop menu entry for the given command (a list of arguments). Named
    after the game, like the Windows shortcuts."""
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={common.GAME_NAME}",
        "GenericName=Roblox game",
        f"Comment=Play {common.GAME_NAME} (in Sober) and show it as your Discord activity",
        f"Exec={_desktop_exec(args)}",
        f"Icon={common.APP_ID}",
        "Terminal=false",
        "Categories=Game;",
        f"Keywords=Roblox;Sober;Discord;{common.GAME_NAME};",
        "StartupNotify=false",
        "",
    ])


def is_integrated():
    return not IS_WINDOWS and os.path.isfile(desktop_entry_path())


def _write_if_changed(path, data):
    """True if the file was (re)written."""
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)
    return True


def _install_icon(png):
    """Put the icon in the user's icon theme (any old size of ours is replaced). True if it changed."""
    width, height = struct.unpack(">II", png[16:24])   # from the PNG's IHDR chunk
    path = os.path.join(_icons_root(), f"{width}x{height}", "apps", common.APP_ID + ".png")
    for old in glob.glob(os.path.join(_icons_root(), "*", "apps", common.APP_ID + ".png")):
        if old != path:
            os.remove(old)
    if not _write_if_changed(path, png):
        return False
    try:
        os.utime(_icons_root())   # icon theme caches are invalidated by the theme folder's mtime
    except OSError:
        pass
    return True


def _bundled_icon():
    """The icon packed into the AppImage (from build time), or None."""
    appdir = os.environ.get("APPDIR", "")
    try:
        with open(os.path.join(appdir, common.APP_ID + ".png"), "rb") as f:
            return f.read()
    except OSError:
        return None


def integrate():
    """--integrate: add the menu entry and the icon (the only files this program ever writes on
    Linux). From then on every launch keeps both up to date."""
    png = None
    try:
        png = common.download_icon_png()
    except Exception as e:
        common.log(f"Couldn't download the game icon ({e}); using the one from the AppImage.")
    png = png or _bundled_icon()
    if png:
        _install_icon(png)
        common.log(f"Icon: {os.path.join(_icons_root(), '<size>', 'apps', common.APP_ID + '.png')}")
    _write_if_changed(desktop_entry_path(), desktop_entry(launch_command()).encode())
    common.log(f"Menu entry: {desktop_entry_path()}")
    if not os.environ.get("APPIMAGE"):
        common.log("(Not running as an AppImage: the menu entry starts this copy of the program.)")


def refresh_integration():
    """Every launch, if integrated: follow the AppImage if it was moved, and update the icon if the
    game icon changed."""
    if not is_integrated():
        return
    if os.environ.get("APPIMAGE"):
        if _write_if_changed(desktop_entry_path(), desktop_entry(launch_command()).encode()):
            common.log("Menu entry updated to point at this AppImage.")
    png = common.download_icon_png()
    if png:
        common.log("Icon updated." if _install_icon(png) else "Icon is already up to date.")


# ------------------------------ Single instance ------------------------------

class _WindowsInstance:
    """A named mutex/event pair (see common.acquire_instance_lock) and a process handle to watch."""

    def __init__(self, event):
        self.event = event
        self.handle, self.pid, self.exited_pid = None, None, None

    @classmethod
    def acquire(cls, timeout=10.0):
        event = common.acquire_instance_lock(timeout)
        return cls(event) if event is not None else None

    def watch(self, pid):
        """Wake wait() when this process ends. False if it can't be watched (wait() then just polls)."""
        if pid != self.pid:
            common.close_handle(self.handle)
            self.handle = common.open_process_handle(pid) if pid else None
            self.pid = pid
        return self.handle is not None

    def wait(self, timeout):
        handles = [self.event] + ([self.handle] if self.handle else [])
        idx = common.wait_any(handles, timeout * 1000)
        if idx == 0:
            return STOP
        if idx == 1:
            common.close_handle(self.handle)
            self.exited_pid, self.handle, self.pid = self.pid, None, None
            return EXITED
        if idx == -2:
            time.sleep(timeout)
        return None

    def close(self):
        common.close_handle(self.handle)


class _LinuxInstance:
    """
    The running instance listens on an abstract Unix socket (it lives only in the kernel: no file
    anywhere), named per user. Being able to bind it means we're the only instance; a new instance
    connects to it to ask the old one to exit, and takes over. SIGTERM, SIGINT and SIGHUP also make
    it exit cleanly.
    """

    ADDRESS = f"\0{common.APP_ID}.{os.getuid() if hasattr(os, 'getuid') else 0}"

    def __init__(self, server, wake_fd):
        self.server, self.wake_fd = server, wake_fd
        self.pid, self.pidfd, self.exited_pid = None, None, None

    @classmethod
    def _bind_within(cls, server, timeout):
        deadline = time.monotonic() + timeout
        while True:
            try:
                server.bind(cls.ADDRESS)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.1)

    @classmethod
    def _ask_to_stop(cls):
        """Ask the running instance to exit; returns its pid (or None)."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                s.connect(cls.ADDRESS)
                pid, _, _ = struct.unpack("3i", s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                             struct.calcsize("3i")))
                s.sendall(b"stop\n")
                return pid
        except OSError:
            return None

    @classmethod
    def acquire(cls, timeout=10.0):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if not cls._bind_within(server, 0):
            common.log("Another instance is running; asking it to hand over.")
            pid = cls._ask_to_stop()
            if not cls._bind_within(server, timeout):
                common.log("The previous instance didn't exit in time; closing it forcefully.")
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                if not cls._bind_within(server, 5):
                    server.close()
                    return None
        server.listen(4)
        server.setblocking(False)

        # Signals: the C-level handler writes to this pipe, which wakes wait()'s select()
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
        signal.set_wakeup_fd(write_fd)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda *_: None)
        return cls(server, read_fd)

    def watch(self, pid):
        """Wake wait() when this process ends. False if it can't be watched (wait() then polls)."""
        if pid != self.pid:
            self._close_pidfd()
            self.pid = pid
            if pid and hasattr(os, "pidfd_open"):
                try:
                    self.pidfd = os.pidfd_open(pid)
                except OSError:
                    self.pidfd = None
        return self.pidfd is not None

    def _handle_connection(self):
        """True if another program of the same user asked us to exit."""
        try:
            conn, _ = self.server.accept()
        except OSError:
            return False
        with conn:
            try:
                conn.settimeout(2)
                # Abstract sockets have no file permissions: only listen to our own user
                _, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                                struct.calcsize("3i")))
                return uid == os.getuid() and conn.recv(16).startswith(b"stop")
            except OSError:
                return False

    def wait(self, timeout):
        fds = [self.server, self.wake_fd] + ([self.pidfd] if self.pidfd is not None else [])
        readable, _, _ = select.select(fds, [], [], timeout)
        if self.wake_fd in readable:
            try:
                while os.read(self.wake_fd, 64):
                    pass
            except OSError:
                pass
            return STOP
        if self.server in readable and self._handle_connection():
            return STOP
        if self.pidfd is not None and self.pidfd in readable:
            self._close_pidfd()
            self.exited_pid, self.pid = self.pid, None
            return EXITED
        if self.pid and self.pidfd is None and not os.path.exists(f"/proc/{self.pid}"):
            self.exited_pid, self.pid = self.pid, None   # no pidfd support: polled instead
            return EXITED
        return None

    def _close_pidfd(self):
        if self.pidfd is not None:
            os.close(self.pidfd)
            self.pidfd = None

    def close(self):
        self._close_pidfd()
        self.server.close()   # frees the name for the next instance


Instance = _WindowsInstance if IS_WINDOWS else _LinuxInstance
