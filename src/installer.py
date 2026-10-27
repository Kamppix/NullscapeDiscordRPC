"""Installer (wizard) for the app."""

import datetime
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import winreg

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import common

SETUP_TITLE = common.APP_NAME + " Setup"


# ============================== Install logic (no UI) ==============================

def payload_dir():
    return common.resource_path("payload")


def interactive_user():
    """The logged-on user (owner of explorer.exe), even if a different admin approved the UAC prompt."""
    try:
        import psutil
        for p in psutil.process_iter(["name", "username"]):
            if (p.info["name"] or "").lower() == "explorer.exe" and p.info["username"]:
                return p.info["username"]
    except Exception:
        pass
    dom, user = os.environ.get("USERDOMAIN"), os.environ.get("USERNAME")
    return f"{dom}\\{user}" if dom and user else user


def grant_user_access(folder):
    """Let the normal (non-admin) user write inside the folder, e.g. Program Files\\<APP_NAME>,
    so the app can keep its log and icon there. Silently ignored where ACLs don't exist (USB sticks)."""
    user = interactive_user()
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


def write_registry(install_dir, icon_path, size_kb):
    uninstall_exe = os.path.join(install_dir, common.UNINSTALL_EXE)
    display_icon = icon_path or os.path.join(install_dir, common.APP_EXE)
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, common.UNINSTALL_REG_KEY, 0, winreg.KEY_WRITE) as k:
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


def registry_entry_exists():
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, common.UNINSTALL_REG_KEY))
        return True
    except OSError:
        return False


def delete_registry():
    try:
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, common.UNINSTALL_REG_KEY)
    except OSError:
        pass


def install(opts, status, progress):
    """
    opts: dir, dir_shortcut, desktop, start_menu, all_users
    Returns {"warnings": [...], "icon": path_or_None}. On a hard failure everything done so far is
    rolled back and the exception is re-raised.
    """
    d = opts["dir"]
    result = {"warnings": [], "icon": None}
    created_dir = not os.path.isdir(d)
    made_shortcuts = []                      # only ones this run created (a failed re-install must
    created_registry = not registry_entry_exists()   # not wipe the previous working install)

    try:
        status(f"Closing any running copy of {common.APP_NAME}...")
        common.stop_app(d)
        progress(0.02)

        status("Creating the installation folder...")
        os.makedirs(d, exist_ok=True)
        grant_user_access(d)
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
            wanted.append(("desktop", common.known_folder(common.CSIDL_DESKTOPDIRECTORY)))
        if opts["start_menu"]:
            if opts["all_users"]:
                wanted.append(("Start Menu (all users)", common.known_folder(common.CSIDL_COMMON_PROGRAMS)))
            else:
                wanted.append(("Start Menu", common.known_folder(common.CSIDL_PROGRAMS)))
        for label, folder in wanted:
            status(f"Creating the {label} shortcut...")
            try:
                if not folder:
                    raise RuntimeError("folder not found")
                lnk = os.path.join(folder, common.SHORTCUT_NAME)
                already_there = os.path.exists(lnk)
                common.create_shortcut(lnk, target, d, ico)
                if not already_there:
                    made_shortcuts.append(lnk)
            except Exception as e:
                result["warnings"].append(f"Couldn't create the {label} shortcut: {e}")
        progress(0.92)

        # ---- Add or Remove Programs entry ----
        status("Registering with Windows...")
        write_registry(d, ico, folder_size_kb(d))
        common.notify_icon_change()
        progress(1.0)
        status("Done.")
        return result

    except Exception:
        for lnk in made_shortcuts:
            try:
                os.remove(lnk)
            except OSError:
                pass
        if created_registry:
            delete_registry()
        if created_dir:
            shutil.rmtree(d, ignore_errors=True)
        raise


def launch_without_elevation(exe):
    """The installer is elevated; starting the app through Explorer runs it as the normal user."""
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

        root.title(SETUP_TITLE)
        root.resizable(False, False)
        w, h = 560, 470
        root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass

        self.var_dir = tk.StringVar(value=common.default_install_dir())
        self.var_dir_shortcut = tk.BooleanVar(value=False)
        self.var_desktop = tk.BooleanVar(value=True)
        self.var_start = tk.BooleanVar(value=False)
        self.var_all_users = tk.BooleanVar(value=False)
        self.var_launch = tk.BooleanVar(value=True)
        self.var_delete_setup = tk.BooleanVar(value=False)
        self.var_status = tk.StringVar()
        self.done = False

        self.page_options = self._build_options()
        self.page_progress = self._build_progress()
        self.page_done = self._build_done()
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
                                       text="Add for all users")
        self.chk_all.pack(anchor="w", padx=(24, 0), pady=2)
        self.chk_all.state(["disabled"])
        ttk.Checkbutton(sc, variable=self.var_dir_shortcut,
                        text="Installation folder shortcut").pack(anchor="w", pady=2)

        btns = ttk.Frame(f)
        btns.pack(side="bottom", fill="x", pady=(18, 0))
        ttk.Button(btns, text="Cancel", command=self.root.destroy).pack(side="right")
        ttk.Button(btns, text="Install", command=self.on_install).pack(side="right", padx=8)
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
        self.lbl_done.pack(anchor="w", pady=(14, 8))
        self.lbl_warn = ttk.Label(f, wraplength=510, justify="left", foreground="#9a5b00")
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

    def on_install(self):
        d = common.normalize_install_dir(self.var_dir.get())
        self.var_dir.set(d)
        drive = os.path.splitdrive(d)[0]
        if drive and not os.path.exists(drive + "\\"):
            messagebox.showerror(SETUP_TITLE, f"The drive {drive} doesn't exist.")
            return
        if os.path.isdir(d) and os.listdir(d) and not os.path.isfile(os.path.join(d, common.APP_EXE)):
            if not messagebox.askyesno(
                    SETUP_TITLE,
                    f"The folder already exists and contains other files:\n\n{d}\n\n"
                    "Uninstalling later removes this entire folder. Install here anyway?"):
                return

        opts = {
            "dir": d,
            "dir_shortcut": self.var_dir_shortcut.get(),
            "desktop": self.var_desktop.get(),
            "start_menu": self.var_start.get(),
            "all_users": self.var_start.get() and self.var_all_users.get(),
        }
        self.install_dir = d
        self.installing = True
        self.bar["value"] = 0
        self.show(self.page_progress)
        threading.Thread(target=self._worker, args=(opts,), daemon=True).start()
        self.root.after(100, self._poll)

    def _worker(self, opts):
        try:
            result = install(opts, status=lambda t: self.q.put(("status", t)),
                             progress=lambda f: self.q.put(("progress", f)))
            self.q.put(("done", result))
        except Exception as e:
            self.q.put(("error", str(e) or e.__class__.__name__))

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
                    self.lbl_done.config(text=f"{common.APP_NAME} was installed to:\n{self.install_dir}\n\n"
                                              "You can remove it any time through Windows Settings > Apps > Installed apps.")
                    self.lbl_warn.config(text="\n".join("- " + w for w in val["warnings"]))
                    self.show(self.page_done)
                    return
                elif kind == "error":
                    self.installing = False
                    messagebox.showerror(SETUP_TITLE,
                                         f"Installation failed and was rolled back:\n\n{val}")
                    self.show(self.page_options)
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def on_finish(self):
        if self.var_launch.get() and self.install_dir:
            launch_without_elevation(os.path.join(self.install_dir, common.APP_EXE))
        self.close()

    def on_close(self):
        if not self.installing:
            self.close()

    def close(self):
        if self.done and self.var_delete_setup.get() and getattr(sys, "frozen", False):
            common.delete_after_exit(sys.executable)   # the setup exe is locked until we exit
        self.root.destroy()


def main():
    if not common.ensure_admin():
        return
    common.set_dpi_aware()
    root = tk.Tk()
    common.set_window_icon(root)
    InstallerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
    # Exit right away instead of waiting on interpreter shutdown, so "Delete installer" (which
    # waits for this process) happens as soon as the window closes.
    os._exit(0)
