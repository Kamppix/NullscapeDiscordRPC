"""Uninstaller (wizard) for the app. Lives in the installation folder as uninstall.exe."""

import os
import queue
import shutil
import sys
import threading
import time
import winreg

import tkinter as tk
from tkinter import messagebox, ttk

import common

UNINSTALL_TITLE = common.APP_NAME + " Uninstall"


# ============================== Uninstall logic (no UI) ==============================

def find_install_dir():
    """The folder this uninstall.exe sits in. (Running from source requires --dir <folder>.)"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    if "--dir" in sys.argv:
        return os.path.abspath(sys.argv[sys.argv.index("--dir") + 1])
    return None


def detect_shortcuts(install_dir):
    """Every shortcut of ours that actually exists (and points at this install): [(label, path)]."""
    existing = [(label, p) for label, p in common.shortcut_locations() if os.path.isfile(p)]
    targets = common.read_shortcut_targets([p for _, p in existing])
    return [(label, p) for label, p in existing if common.shortcut_is_ours(p, targets, install_dir)]


def delete_registry():
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            winreg.DeleteKey(hive, common.UNINSTALL_REG_KEY)
        except OSError:
            pass


def remove_with_retry(path, tries=6):
    for i in range(tries):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if i == tries - 1:
                raise
            time.sleep(0.5)


def spawn_self_delete(install_dir):
    """uninstall.exe can't delete itself while running, so a hidden helper waits until every
    uninstall.exe process has exited (however long the window stays open), then removes the
    now unlocked folder."""
    os.chdir(os.environ.get("SystemRoot", "C:\\Windows"))   # our own CWD must not be inside the folder
    common.delete_after_exit(install_dir)


def uninstall(install_dir, status, progress):
    """Returns a list of warnings. Refuses to touch anything that doesn't look like our folder."""
    if not common.is_valid_install_dir(install_dir):
        raise RuntimeError(f"This doesn't look like a {common.APP_NAME} installation folder; nothing was removed.")
    warnings = []

    status(f"Closing {common.APP_NAME} if it's running...")
    common.stop_app(install_dir)
    progress(0.1)

    shortcuts = detect_shortcuts(install_dir)
    for i, (label, path) in enumerate(shortcuts):
        status(f"Removing the {label} shortcut...")
        try:
            os.remove(path)
        except OSError as e:
            warnings.append(f"Couldn't remove the {label} shortcut: {e}")
        progress(0.1 + 0.2 * (i + 1) / max(1, len(shortcuts)))
    progress(0.3)

    status("Removing it from Add or Remove Programs...")
    delete_registry()
    progress(0.4)

    status("Deleting program files...")
    own_name = common.UNINSTALL_EXE.lower()
    entries = [e for e in os.listdir(install_dir) if e.lower() != own_name]
    for i, name in enumerate(entries):
        try:
            remove_with_retry(os.path.join(install_dir, name))
        except OSError as e:
            warnings.append(f"Couldn't delete {name}: {e}")
        progress(0.4 + 0.5 * (i + 1) / max(1, len(entries)))

    status("Finishing up...")
    spawn_self_delete(install_dir)   # removes uninstall.exe and the folder itself once we exit
    common.notify_icon_change()
    progress(1.0)
    return warnings


# ============================== UI ==============================

class UninstallerApp:
    def __init__(self, root, install_dir):
        self.root = root
        self.install_dir = install_dir
        self.q = queue.Queue()
        self.working = False

        root.title(UNINSTALL_TITLE)
        root.resizable(False, False)
        w, h = 540, 400
        root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass

        self.var_status = tk.StringVar()
        self.page_confirm = self._build_confirm()
        self.page_progress = self._build_progress()
        self.page_done = self._build_done()
        self.show(self.page_confirm)

    def _build_confirm(self):
        f = ttk.Frame(self.root, padding=20)
        ttk.Label(f, text=f"Uninstall {common.APP_NAME}", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(f, wraplength=490, justify="left", text="Are you sure you want to uninstall?").pack(anchor="w", pady=(10, 4))
        ttk.Label(f, wraplength=490, justify="left", text="This will remove:").pack(anchor="w", pady=(10, 4))
        self.shortcuts = detect_shortcuts(self.install_dir)
        lines = [f"- the installation folder  {self.install_dir}", "- the Windows app list entry"]
        lines += [f"- the {label} shortcut" for label, _ in self.shortcuts]
        ttk.Label(f, wraplength=490, justify="left", text="\n".join(lines)).pack(anchor="w", padx=(10, 0))
        btns = ttk.Frame(f)
        btns.pack(side="bottom", fill="x")
        ttk.Button(btns, text="Cancel", command=self.root.destroy).pack(side="right")
        ttk.Button(btns, text="Uninstall", command=self.on_uninstall).pack(side="right", padx=8)
        return f

    def _build_progress(self):
        f = ttk.Frame(self.root, padding=20)
        ttk.Label(f, text="Uninstalling...", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(f, textvariable=self.var_status, wraplength=490).pack(anchor="w", pady=(20, 8))
        self.bar = ttk.Progressbar(f, maximum=100, length=480)
        self.bar.pack(anchor="w")
        return f

    def _build_done(self):
        f = ttk.Frame(self.root, padding=20)
        ttk.Label(f, text=f"{common.APP_NAME} has been uninstalled", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        self.lbl_done = ttk.Label(f, wraplength=490, justify="left")
        self.lbl_done.pack(anchor="w", pady=(14, 0))
        btns = ttk.Frame(f)
        btns.pack(side="bottom", fill="x")
        ttk.Button(btns, text="Close", command=self.root.destroy).pack(side="right")
        return f

    def show(self, page):
        for p in (self.page_confirm, self.page_progress, self.page_done):
            p.pack_forget()
        page.pack(fill="both", expand=True)

    def on_uninstall(self):
        self.working = True
        self.show(self.page_progress)
        threading.Thread(target=self._worker, daemon=True).start()
        self.root.after(100, self._poll)

    def _worker(self):
        try:
            warnings = uninstall(self.install_dir, status=lambda t: self.q.put(("status", t)),
                                 progress=lambda f: self.q.put(("progress", f)))
            self.q.put(("done", warnings))
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
                    self.working = False
                    text = "The program was fully removed from your system.\n\nThe uninstaller will also be gone after you close this window."
                    if val:
                        text += "\n\nSome things need attention:\n" + "\n".join("- " + w for w in val)
                    self.lbl_done.config(text=text)
                    self.show(self.page_done)
                    return
                elif kind == "error":
                    self.working = False
                    messagebox.showerror(UNINSTALL_TITLE, val)
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def on_close(self):
        if not self.working:
            self.root.destroy()


def main():
    if not common.ensure_admin():
        return
    common.set_dpi_aware()
    d = find_install_dir()
    if not d or not common.is_valid_install_dir(d):
        root = tk.Tk()
        root.withdraw()
        common.set_window_icon(root)
        messagebox.showerror(UNINSTALL_TITLE,
                             f"This doesn't look like a {common.APP_NAME} installation folder, so nothing was removed.")
        return
    root = tk.Tk()
    common.set_window_icon(root)
    UninstallerApp(root, d)
    root.mainloop()


if __name__ == "__main__":
    main()
    # Exit right away instead of waiting on interpreter shutdown: the self-delete helper is waiting
    # for this process, and a lingering uninstall.exe keeps the folder locked.
    os._exit(0)
