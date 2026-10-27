"""Helpers shared by the app, installer and uninstaller."""

import base64
import ctypes
import io
import ntpath
import os
import subprocess
import sys

GAME_NAME = "Nullscape"               # the only place either name is spelled out
APP_NAME = GAME_NAME + "DiscordRPC"   # program, folder, exe and log names
APP_EXE = APP_NAME + ".exe"
UNINSTALL_EXE = "uninstall.exe"
APP_VERSION = "1.0.0"
PUBLISHER = "Kamppix"
SHORTCUT_NAME = GAME_NAME + ".lnk"   # shortcuts are named after the game, not the program

PLACE_ID = 129279692364812
UNIVERSE_ID = 7623259179

UNINSTALL_REG_KEY = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" + APP_NAME

MUTEX_NAME = "Local\\" + APP_NAME + "_Instance"
EVENT_NAME = "Local\\" + APP_NAME + "_TakeOver"

CREATE_NO_WINDOW = 0x08000000

CSIDL_PROGRAMS = 0x0002
CSIDL_DESKTOPDIRECTORY = 0x0010
CSIDL_COMMON_PROGRAMS = 0x0017
CSIDL_COMMON_DESKTOPDIRECTORY = 0x0019

# Each program sets this to its own logger (the installer/uninstaller leave it silent)
log_fn = lambda msg: None


def log(msg):
    log_fn(msg)


# ------------------------------ Paths ------------------------------

def resource_path(name):
    """A file bundled into the exe at build time (or next to this script when run from source)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def default_install_dir():
    return ntpath.join(os.environ.get("ProgramFiles") or r"C:\Program Files", APP_NAME)


def normalize_install_dir(path):
    """Make sure the install directory always ends in ...\\APP_NAME"""
    p = os.path.expandvars((path or "").strip().strip('"'))
    if not p:
        p = default_install_dir()
    p = ntpath.abspath(p)
    if len(p) > 3:
        p = p.rstrip("\\/")
    if ntpath.basename(p).lower() != APP_NAME.lower():
        p = ntpath.join(p, APP_NAME)
    return p


def is_valid_install_dir(path):
    """Safety check before anything is deleted: right name, and our exe is inside."""
    return (
        bool(path)
        and len(path) > 3
        and ntpath.basename(path.rstrip("\\/")).lower() == APP_NAME.lower()
        and os.path.isfile(os.path.join(path, APP_EXE))
    )


def known_folder(csidl):
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
        return buf.value
    return None


def shortcut_locations():
    """Every place outside the installation folder where the installer may have put a shortcut:
    [(label, full_path)]. (The one inside the folder is simply deleted along with it.)"""
    spots = [
        ("desktop", known_folder(CSIDL_DESKTOPDIRECTORY)),
        ("desktop (all users)", known_folder(CSIDL_COMMON_DESKTOPDIRECTORY)),
        ("Start Menu", known_folder(CSIDL_PROGRAMS)),
        ("Start Menu (all users)", known_folder(CSIDL_COMMON_PROGRAMS)),
    ]
    return [(label, os.path.join(folder, SHORTCUT_NAME)) for label, folder in spots if folder]


# ------------------------------ Windows helpers ------------------------------

def ensure_admin():
    """True if already elevated. Otherwise re-launches elevated and returns False."""
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return True
    except Exception:
        return True
    if getattr(sys, "frozen", False):
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        params = subprocess.list2cmdline([os.path.abspath(sys.argv[0])] + sys.argv[1:])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    return False


def set_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


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


def delete_after_exit(path):
    """Delete a file or folder (recursively) once this program has exited."""
    run_after_exit(
        "$t = '%s';"
        "for ($i = 0; $i -lt 60; $i++) {"
        " Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue;"
        " if (-not (Test-Path -LiteralPath $t)) { break };"
        " Start-Sleep -Milliseconds 500"
        "}" % _q(path))


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
    if not ask_running_instance_to_exit(10.0):
        kill_app_processes(install_dir)
