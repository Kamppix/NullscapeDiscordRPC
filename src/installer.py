"""Installer (wizard) for the app."""

import base64
import datetime
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import winreg

import tkinter as tk
from tkinter import filedialog, ttk

import common

SETUP_TITLE = common.APP_NAME + " Setup"
ELEVATED_INSTALL_ARG = "--elevated-install"   # + the chosen options, for the elevated copy


# ============================== Install logic (no UI) ==============================

def payload_dir():
    return common.resource_path("payload")


def grant_user_access(folder):
    """Let the normal (non-admin) user write inside the folder, e.g. Program Files\\<APP_NAME>,
    so the app can keep its log and icon there. Silently ignored where ACLs don't exist (USB sticks)."""
    user = common.interactive_user()
    if not user:
        return False
    try:
        r = subprocess.run(["icacls", folder, "/grant", f"{user}:(OI)(CI)M"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           creationflags=common.CREATE_NO_WINDOW, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def copy_with_retry(src, dst, tries=6):
    for i in range(tries):
        try:
            shutil.copy2(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.5)


def folder_size_kb(folder):
    total = 0
    for root, _, names in os.walk(folder):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return max(1, total // 1024)


def registry_hive():
    """Where the "installed apps" entry goes: for the whole machine when we're elevated, otherwise
    just for this user (Windows lists both)."""
    return winreg.HKEY_LOCAL_MACHINE if common.is_admin() else winreg.HKEY_CURRENT_USER


def needs_admin(opts):
    """Does installing with these options need administrator rights? Only for a folder we can't
    write to (e.g. Program Files), a Start Menu shortcut for all users, or to update an existing
    installation in that folder that's registered for the whole machine, or to remove an old
    installation (being moved here) that needs them to be removed."""
    machine_wide_here = common.same_path(common.registered_location(winreg.HKEY_LOCAL_MACHINE), opts["dir"])
    return (not common.can_write(opts["dir"]) or opts["all_users"] or machine_wide_here
            or any(common.removal_needs_admin(old, common.installed_shortcuts(old))
                   for old in opts.get("replace", [])))


def write_registry(hive, install_dir, icon_path, size_kb):
    uninstall_exe = os.path.join(install_dir, common.UNINSTALL_EXE)
    display_icon = icon_path or os.path.join(install_dir, common.APP_EXE)
    with winreg.CreateKeyEx(hive, common.UNINSTALL_REG_KEY, 0, winreg.KEY_WRITE) as k:
        for name, value in (
            ("DisplayName", common.APP_NAME),
            ("DisplayVersion", common.APP_VERSION),
            ("Publisher", common.PUBLISHER),
            ("InstallLocation", install_dir),
            ("DisplayIcon", display_icon),
            ("UninstallString", f'"{uninstall_exe}"'),
            ("InstallDate", datetime.date.today().strftime("%Y%m%d")),
        ):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        for name, value in (("NoModify", 1), ("NoRepair", 1), ("EstimatedSize", size_kb)):
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, value)


def registry_entry_exists(hive):
    try:
        winreg.CloseKey(winreg.OpenKey(hive, common.UNINSTALL_REG_KEY))
        return True
    except OSError:
        return False


def delete_registry(hive):
    try:
        winreg.DeleteKey(hive, common.UNINSTALL_REG_KEY)
    except OSError:
        pass


def set_aside_old_installation(old):
    """Move an old installation (one being moved to the new folder) out of the way in one rename,
    which fails just like deleting it would (a file in use, access denied): then the install
    stops before anything was changed. It's deleted only once the new one is installed, and put
    back if installing fails. Returns (the folder it was moved to, its shortcuts)."""
    shortcuts = common.installed_shortcuts(old)   # (found by where they point, so before moving it)
    aside = old + ".old"
    n = 1
    while os.path.exists(aside):
        n += 1
        aside = f"{old}.old{n}"
    try:
        for i in range(6):   # files can stay locked for a moment after the app exits
            try:
                os.rename(old, aside)
                break
            except OSError:
                if i == 5:
                    raise
                time.sleep(0.5)
    except OSError as e:
        common.log(f"Couldn't move {old} aside: {e}")
        raise common.PlainError(f"The old installation in {old} couldn't be removed "
                           f"({common.friendly_error(e)}). If anything in that folder is open, "
                           "close it and try again.") from e
    return aside, shortcuts


def remove_old_installation(old, aside, shortcuts, new_shortcuts):
    """Finish removing an old installation once the new one is installed: its shortcuts (except
    ones the new installation just took over), its "installed apps" entry, and its files. Returns
    warnings."""
    warnings = []
    for label, path in shortcuts:
        if any(common.same_path(path, new) for new in new_shortcuts):
            continue
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            common.log(f"Couldn't remove {path}: {e}")
            warnings.append(f"Couldn't remove the old {label} shortcut: {common.friendly_error(e)}")
    common.delete_registry_entries(old)
    try:
        common.delete_tree(aside)
    except OSError as e:
        common.log(f"Couldn't delete {aside} yet ({e}); deleting it once the installer exits.")
        common.delete_after_exit(aside)
    return warnings


def install(opts, status, progress):
    """
    opts: dir, dir_shortcut, desktop, start_menu, all_users, replace (old installation folders to
    remove: this one replaces them)
    Returns {"warnings": [...], "icon": path_or_None}. On a hard failure everything done so far is
    rolled back and the exception is re-raised.
    """
    d = opts["dir"]
    result = {"warnings": [], "icon": None}
    created_dir = not os.path.isdir(d)
    hive = registry_hive()
    made_shortcuts = []                      # only ones this run created (a failed re-install must
    created_registry = not registry_entry_exists(hive)   # not wipe the previous working install)
    new_shortcuts = []                       # all shortcuts of the new installation
    set_aside = []                           # [(old folder, where it was moved, its shortcuts)]

    try:
        status(f"Closing any running copy of {common.APP_NAME}...")
        common.stop_app(d)
        for old in opts.get("replace", []):
            common.stop_app(old)
        progress(0.02)

        for old in opts.get("replace", []):
            status(f"Removing the old installation in {old}...")
            set_aside.append((old, *set_aside_old_installation(old)))

        status("Creating the installation folder...")
        os.makedirs(d, exist_ok=True)
        if common.is_admin():
            grant_user_access(d)   # e.g. Program Files: let the app (run as the user) update its icon
        progress(0.05)

        # ---- copy the program files ----
        src = payload_dir()
        files = []
        for root, _, names in os.walk(src):
            for n in names:
                full = os.path.join(root, n)
                files.append((full, os.path.relpath(full, src)))
        rels = {r.lower() for _, r in files}
        if common.APP_EXE.lower() not in rels or common.UNINSTALL_EXE.lower() not in rels:
            raise RuntimeError("The installer is incomplete (program files are missing).")

        for i, (full, rel) in enumerate(files):
            dest = os.path.join(d, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            copy_with_retry(full, dest)
            if i % 15 == 0 or i == len(files) - 1:
                status(f"Copying files ({i + 1} of {len(files)})...")
            progress(0.05 + 0.65 * (i + 1) / len(files))

        # ---- initial game icon -> icon.ico (used by all shortcuts) ----
        status("Downloading the game icon...")
        ico, _ = common.refresh_icon(d)
        if not ico and os.path.isfile(common.resource_path("icon.png")):
            # Offline: use the icon from build time; the app updates it on its first launch
            try:
                with open(common.resource_path("icon.png"), "rb") as f:
                    ico_bytes = common.png_bytes_to_ico(f.read())
                with open(os.path.join(d, "icon.ico"), "wb") as f:
                    f.write(ico_bytes)
                ico = os.path.join(d, "icon.ico")
            except Exception:
                pass
        result["icon"] = ico
        if not ico:
            result["warnings"].append(
                f"Couldn't download the game icon (no internet?). {common.APP_NAME} will fetch it on its first launch.")
        else:
            # The exes still carry the icon from build time; give them the current one
            with open(ico, "rb") as f:
                ico_bytes = f.read()
            for exe in (common.APP_EXE, common.UNINSTALL_EXE):
                try:
                    common.set_exe_icon(os.path.join(d, exe), ico_bytes)
                except Exception:
                    pass   # cosmetic only: the app retries whenever the game icon changes
        progress(0.82)

        # ---- shortcuts (only the ones that were ticked) ----
        target = os.path.join(d, common.APP_EXE)
        wanted = []
        if opts["dir_shortcut"]:
            wanted.append(("installation folder", d))
        if opts["desktop"]:
            # Always the logged-on user's own desktop, also for an all-users installation
            wanted.append(("desktop", common.user_folder(common.CSIDL_DESKTOPDIRECTORY)))
        if opts["start_menu"]:
            if opts["all_users"]:
                wanted.append(("Start Menu (all users)", common.known_folder(common.CSIDL_COMMON_PROGRAMS)))
            else:
                wanted.append(("Start Menu", common.user_folder(common.CSIDL_PROGRAMS)))
        for label, folder in wanted:
            status(f"Creating the {label} shortcut...")
            try:
                if not folder:
                    raise RuntimeError("folder not found")
                lnk = os.path.join(folder, common.SHORTCUT_NAME)
                already_there = os.path.exists(lnk)
                common.create_shortcut(lnk, target, d, ico)
                new_shortcuts.append(lnk)
                if not already_there:
                    made_shortcuts.append(lnk)
            except Exception as e:
                common.log(f"Couldn't create the {label} shortcut: {e}")
                result["warnings"].append(f"Couldn't create the {label} shortcut: {common.friendly_error(e)}")
        progress(0.92)

        # ---- Add or Remove Programs entry ----
        status("Registering with Windows...")
        write_registry(hive, d, ico, folder_size_kb(d))
        progress(0.96)

        # ---- the old installations this one replaces ----
        for old, aside, shortcuts in set_aside:
            status(f"Removing the old installation in {old}...")
            result["warnings"] += remove_old_installation(old, aside, shortcuts, new_shortcuts)
        set_aside = []
        common.notify_icon_change()
        progress(1.0)
        status("Done.")
        return result

    except Exception:
        for old, aside, shortcuts in reversed(set_aside):
            try:
                os.rename(aside, old)
            except OSError as e:
                common.log(f"Couldn't move {aside} back to {old}: {e}")
                continue
            for _, lnk in shortcuts:   # the ones the new installation took over point back again
                if any(common.same_path(lnk, new) for new in new_shortcuts):
                    try:
                        icon = os.path.join(old, "icon.ico")
                        common.create_shortcut(lnk, os.path.join(old, common.APP_EXE), old,
                                               icon if os.path.isfile(icon) else None)
                    except Exception as e:
                        common.log(f"Couldn't restore {lnk}: {e}")
        for lnk in made_shortcuts:
            try:
                os.remove(lnk)
            except OSError:
                pass
        if created_registry:
            delete_registry(hive)
        if created_dir:
            shutil.rmtree(d, ignore_errors=True)
        raise


def launch_without_elevation(exe):
    """Starting the app through Explorer runs it as the normal user, also when we're elevated."""
    try:
        subprocess.Popen(["explorer.exe", exe], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=common.CREATE_NO_WINDOW)
    except Exception:
        pass


# ============================== UI ==============================

class InstallerApp:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.installing = False
        self.install_dir = None
        self.replaced = []
        # Already installed? Then that folder is the one suggested (installing there updates it)
        self.existing = common.installations()
        if self.existing:
            common.log("Already installed in: " + ", ".join(self.existing))

        root.title(SETUP_TITLE)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.colors = common.apply_theme(root)   # follows the system's light/dark setting

        self.var_dir = tk.StringVar(value=self.existing[0] if self.existing else common.default_install_dir())
        self.var_dir_shortcut = tk.BooleanVar(value=False)
        self.var_desktop = tk.BooleanVar(value=True)
        self.var_start = tk.BooleanVar(value=True)
        self.var_all_users = tk.BooleanVar(value=False)
        self.var_launch = tk.BooleanVar(value=True)
        self.var_delete_setup = tk.BooleanVar(value=False)
        self.var_status = tk.StringVar()
        self.done = False
        self.handing_off = False   # waiting for the elevated copy to take over

        self.page_options = self._build_options()
        self.page_progress = self._build_progress()
        self.page_done = self._build_done()
        common.size_window(root, 560, (self.page_options, self.page_progress, self.page_done))
        self.show(self.page_options)

    # ---- pages ----
    def _build_options(self):
        f = ttk.Frame(self.root, padding=20)
        ttk.Label(f, text=f"Install {common.APP_NAME}", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(f, wraplength=510, justify="left", text=(
            "Choose an installation folder and any shortcuts you want to have. "
            "Moving the installation folder afterwards will mess up shortcuts and the "
            "Windows \"installed apps\" list, so if you want to move it afterwards, "
            "it is recommended to instead reinstall the program. "
        )).pack(anchor="w", pady=(6, 14))
        if self.existing:
            ttk.Label(f, wraplength=510, justify="left", text=(
                f"{common.APP_NAME} is already installed in:\n" + "\n".join(self.existing) + "\n"
                "Installing it in the same folder updates it. Choosing another folder moves it there."
            )).pack(anchor="w", pady=(0, 14))

        # Presets for the folder and Start Menu settings below; the one that matches is disabled
        presets = ttk.Frame(f)
        presets.pack(fill="x", pady=(0, 12))
        presets.columnconfigure((0, 1), weight=1, uniform="presets")
        self.btn_for_me = ttk.Button(presets, text="Install only for me", command=self.preset_for_me)
        self.btn_for_me.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_for_all = ttk.Button(presets, text="Install for all users", command=self.preset_for_all)
        self.btn_for_all.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        box = ttk.LabelFrame(f, text="Installation folder", padding=10)
        box.pack(fill="x")
        entry = ttk.Entry(box, textvariable=self.var_dir)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<FocusOut>", lambda e: self.var_dir.set(common.normalize_install_dir(self.var_dir.get())))
        ttk.Button(box, text="Browse...", command=self.browse).pack(side="left", padx=(8, 0))

        sc = ttk.LabelFrame(f, text="Shortcuts", padding=10)
        sc.pack(fill="x", pady=(14, 0))
        ttk.Checkbutton(sc, variable=self.var_desktop, text="Desktop shortcut").pack(anchor="w", pady=2)
        ttk.Checkbutton(sc, variable=self.var_start, text="Start Menu shortcut",
                        command=self.on_start_toggle).pack(anchor="w", pady=2)
        self.chk_all = ttk.Checkbutton(sc, variable=self.var_all_users,
                                       text="For all users")
        self.chk_all.pack(anchor="w", padx=(24, 0), pady=2)
        self.on_start_toggle()   # "For all users" is only available with a Start Menu shortcut
        ttk.Checkbutton(sc, variable=self.var_dir_shortcut,
                        text="Installation folder shortcut").pack(anchor="w", pady=2)

        btns = ttk.Frame(f)
        btns.pack(side="bottom", fill="x", pady=(18, 0))
        ttk.Button(btns, text="Cancel", command=self.root.destroy).pack(side="right")
        ttk.Button(btns, text="Install", command=self.on_install).pack(side="right", padx=8)

        for var in (self.var_dir, self.var_start, self.var_all_users):
            var.trace_add("write", lambda *_: self.update_presets())
        self.update_presets()
        return f

    def _build_progress(self):
        f = ttk.Frame(self.root, padding=20)
        ttk.Label(f, text="Installing...", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(f, textvariable=self.var_status, wraplength=510).pack(anchor="w", pady=(20, 8))
        self.bar = ttk.Progressbar(f, maximum=100, length=500)
        self.bar.pack(anchor="w")
        return f

    def _build_done(self):
        f = ttk.Frame(self.root, padding=20)
        ttk.Label(f, text="Installation complete", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        self.lbl_done = ttk.Label(f, wraplength=510, justify="left")
        self.lbl_done.pack(anchor="w", pady=(14, 16))
        self.lbl_warn = ttk.Label(f, wraplength=510, justify="left", foreground=self.colors["warning"])
        self.lbl_warn.pack(anchor="w")
        btns = ttk.Frame(f)
        btns.pack(side="bottom", fill="x")
        ttk.Button(btns, text="Finish", command=self.on_finish).pack(side="right", anchor="s")
        checks = ttk.Frame(btns)
        checks.pack(side="left")
        ttk.Checkbutton(checks, variable=self.var_launch, text=f"Launch {common.GAME_NAME}").pack(anchor="w")
        if getattr(sys, "frozen", False):   # from source the "installer" would be python.exe
            ttk.Checkbutton(checks, variable=self.var_delete_setup, text="Delete installer").pack(anchor="w", pady=(4, 0))
        return f

    def show(self, page):
        for p in (self.page_options, self.page_progress, self.page_done):
            p.pack_forget()
        page.pack(fill="both", expand=True)

    # ---- handlers ----
    def browse(self):
        chosen = filedialog.askdirectory(title=f"Choose where to install {common.APP_NAME}",
                                         initialdir=os.path.dirname(self.var_dir.get()) or None)
        if chosen:
            self.var_dir.set(common.normalize_install_dir(chosen.replace("/", "\\")))

    def on_start_toggle(self):
        if self.var_start.get():
            self.chk_all.state(["!disabled"])
        else:
            self.var_all_users.set(False)
            self.chk_all.state(["disabled"])

    # ---- presets ----
    def preset_for_me(self):
        """Per-user folder, and a Start Menu shortcut (if one is wanted) just for this user."""
        self.var_dir.set(common.default_install_dir())
        self.var_all_users.set(False)

    def preset_for_all(self):
        """Program Files, and a Start Menu shortcut for all users."""
        self.var_dir.set(common.all_users_install_dir())
        self.var_start.set(True)
        self.on_start_toggle()
        self.var_all_users.set(True)

    def update_presets(self):
        """Disable the preset button whose settings are already in place."""
        folder = common.normalize_install_dir(self.var_dir.get())
        for_all_users = self.var_start.get() and self.var_all_users.get()
        is_for_me = common.same_path(folder, common.default_install_dir()) and not for_all_users
        is_for_all = common.same_path(folder, common.all_users_install_dir()) and for_all_users
        self.btn_for_me.state(["disabled" if is_for_me else "!disabled"])
        self.btn_for_all.state(["disabled" if is_for_all else "!disabled"])

    def on_install(self):
        if self.handing_off:
            return   # the elevated copy is starting
        d = common.normalize_install_dir(self.var_dir.get())
        self.var_dir.set(d)
        drive = os.path.splitdrive(d)[0]
        if drive and not os.path.exists(drive + "\\"):
            common.dialog(self.root, SETUP_TITLE, f"The drive {drive} doesn't exist.")
            return
        reason = common.shared_folder_reason(d)
        if reason:
            common.dialog(self.root, SETUP_TITLE, f"{d} is {reason}. Uninstalling {common.APP_NAME} "
                          "later removes its whole installation folder, so choose a folder of its own, "
                          f"like {common.ntpath.join(d, common.APP_NAME)}.")
            return
        if os.path.isdir(d) and os.listdir(d) and not os.path.isfile(os.path.join(d, common.APP_EXE)):
            answer = common.dialog(self.root, SETUP_TITLE,
                                   f"The folder already exists and contains other files:\n\n{d}\n\n"
                                   "Uninstalling later removes this entire folder. Install here anyway?",
                                   buttons=("Yes", "No"))
            if answer != 0:
                return
        # Only one installation at a time: one in another folder is moved here (no copies that
        # aren't in the "installed apps" list are left behind)
        replace = [old for old in self.existing if not common.same_path(old, d)]
        if replace:
            answer = common.dialog(self.root, SETUP_TITLE,
                                   f"{common.APP_NAME} is already installed in:\n\n" + "\n".join(replace)
                                   + f"\n\nInstalling it in {d} instead moves it there: the old copy is "
                                   "removed once the new one is installed.",
                                   buttons=("Move it", "Cancel"))
            if answer != 0:
                return

        opts = {
            "dir": d,
            "dir_shortcut": self.var_dir_shortcut.get(),
            "desktop": self.var_desktop.get(),
            "start_menu": self.var_start.get(),
            "all_users": self.var_start.get() and self.var_all_users.get(),
            "replace": replace,
        }
        if needs_admin(opts) and not common.is_admin():
            common.log("Installing with these settings needs administrator rights; asking for them.")
            # Continue in an elevated copy of the installer, which installs these choices right away
            payload = base64.urlsafe_b64encode(json.dumps(opts).encode()).decode()
            handoff = common.prepare_handoff()
            if common.relaunch_elevated([ELEVATED_INSTALL_ARG, payload]):
                # This window closes once the elevated copy's window is up
                self.handing_off = True
                common.wait_for_handoff(self.root, handoff)
            else:
                common.log("Administrator permission wasn't given.")
                common.close_handle(handoff)
                common.dialog(self.root, SETUP_TITLE, "Installing with these settings needs administrator "
                              "permission, which wasn't given.\n\nTo install without it, choose "
                              "a folder you can write to (like the default one) and don't add "
                              "the Start Menu shortcut for all users.")
            return
        self.start_install(opts)

    def start_install(self, opts):
        d = opts["dir"]
        self.var_dir.set(d)
        self.install_dir = d
        self.replaced = opts.get("replace", [])
        self.installing = True
        self.bar["value"] = 0
        self.show(self.page_progress)
        threading.Thread(target=self._worker, args=(opts,), daemon=True).start()
        self.root.after(100, self._poll)

    def _worker(self, opts):
        def status(text):
            common.log(text)
            self.q.put(("status", text))
        common.log(f"Installing {common.APP_NAME} {common.APP_VERSION} with: {json.dumps(opts)}")
        try:
            result = install(opts, status=status, progress=lambda f: self.q.put(("progress", f)))
            common.log("Installed." + "".join("\n  " + w for w in result["warnings"]))
            self.q.put(("done", result))
        except Exception as e:
            common.log("Failed and rolled back:\n" + traceback.format_exc())
            self.q.put(("error", common.friendly_error(e)))

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "status":
                    self.var_status.set(val)
                elif kind == "progress":
                    self.bar["value"] = val * 100
                elif kind == "done":
                    self.installing = False
                    self.done = True
                    moved = "".join(f"\n\nThe old copy in {old} was removed." for old in self.replaced)
                    self.lbl_done.config(text=f"{common.APP_NAME} was installed to:\n{self.install_dir}{moved}\n\n"
                                              "You can remove it any time through Windows Settings > Apps > Installed apps.")
                    self.lbl_warn.config(text="\n".join("- " + w for w in val["warnings"]))
                    self.show(self.page_done)
                    return
                elif kind == "error":
                    self.installing = False
                    common.dialog(self.root, SETUP_TITLE, f"Installation failed and was rolled back:\n\n{val}")
                    self.show(self.page_options)
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def on_finish(self):
        """Finish: the only way "Launch" and "Delete installer" take effect (closing the window
        with X just closes it)."""
        if self.var_launch.get() and self.install_dir:
            launch_without_elevation(os.path.join(self.install_dir, common.APP_EXE))
        if self.done and self.var_delete_setup.get() and getattr(sys, "frozen", False):
            common.delete_after_exit(sys.executable)   # the setup exe is locked until we exit
        self.root.destroy()

    def on_close(self):
        if not self.installing:
            self.root.destroy()


def main():
    # No admin rights up front: on_install() asks for them only when the chosen options need them
    common.start_log(common.APP_NAME + "-setup")
    common.set_dpi_aware()
    root = tk.Tk()
    root.withdraw()   # shown by show_window() once built, title bar already in the right mode
    common.set_window_icon(root)
    app = InstallerApp(root)
    common.show_window(root)
    if ELEVATED_INSTALL_ARG in sys.argv:
        # The elevated copy started by on_install(): the original window can close now, and the
        # choices made there are installed right away
        common.signal_handoff()
        try:
            payload = sys.argv[sys.argv.index(ELEVATED_INSTALL_ARG) + 1]
            opts = json.loads(base64.urlsafe_b64decode(payload))
        except (IndexError, ValueError):
            opts = None
        if opts:
            root.after(0, app.start_install, opts)
    root.mainloop()


if __name__ == "__main__":
    main()
    # Exit right away instead of waiting on interpreter shutdown, so "Delete installer" (which
    # waits for this process) happens as soon as the window closes.
    os._exit(0)
