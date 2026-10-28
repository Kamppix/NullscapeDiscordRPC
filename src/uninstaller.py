"""Uninstaller (wizard) for the app. Lives in the installation folder as uninstall.exe."""

import os
import queue
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import ttk

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


STAGING_DIR = ".uninstalling"   # inside the installation folder, see set_aside_program_files()


class UninstallAborted(Exception):
    """The program files couldn't all be removed, so nothing was changed."""


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


def set_aside_program_files(install_dir):
    """Move everything in the installation folder (except this uninstaller) into a staging
    subfolder, one rename each. A rename fails just like a delete would (a file in use, access
    denied), so if anything can't be moved, everything is moved back and UninstallAborted is raised:
    then nothing at all has been changed. Returns the staging folder."""
    staging = os.path.join(install_dir, STAGING_DIR)
    os.makedirs(staging, exist_ok=True)
    own_name = common.UNINSTALL_EXE.lower()
    moved = []
    try:
        for name in os.listdir(install_dir):
            if name.lower() in (own_name, STAGING_DIR):
                continue
            src, dst = os.path.join(install_dir, name), os.path.join(staging, name)
            try:
                retry(lambda: os.rename(src, dst))
            except OSError as e:
                common.log(f"Couldn't move {src}: {e}; moving everything back.")
                raise UninstallAborted(
                    f"{common.APP_NAME} wasn't uninstalled, because {name} couldn't be removed "
                    f"({common.friendly_error(e)}). Nothing was changed.\n\nIf {common.APP_NAME} or "
                    f"anything else in its folder is still open, close it and try again.") from e
            moved.append((src, dst))
    except UninstallAborted:
        for src, dst in reversed(moved):
            try:
                os.rename(dst, src)
            except OSError:
                pass
        try:
            os.rmdir(staging)
        except OSError:
            pass
        raise
    return staging


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
    progress(0.05)

    # All or nothing: if any program file can't be removed, stop before changing anything
    status("Checking that all program files can be removed...")
    staging = set_aside_program_files(install_dir)
    progress(0.1)

    shortcuts = common.installed_shortcuts(install_dir)
    for i, (label, path) in enumerate(shortcuts):
        status(f"Removing the {label} shortcut...")
        try:
            os.remove(path)
        except OSError as e:
            common.log(f"Couldn't remove {path}: {e}")
            warnings.append(f"Couldn't remove the {label} shortcut: {common.friendly_error(e)}")
        progress(0.1 + 0.2 * (i + 1) / max(1, len(shortcuts)))
    progress(0.3)

    status("Removing it from Add or Remove Programs...")
    common.delete_registry_entries(install_dir)
    progress(0.4)

    status("Deleting program files...")
    try:
        retry(lambda: common.delete_tree(staging))
    except OSError as e:
        # whatever is left goes with the rest of the folder once the uninstaller exits
        common.log(f"Some program files are still locked ({e}); they go with the folder.")
    progress(0.9)

    status("Finishing up...")
    spawn_self_delete(install_dir)   # removes uninstall.exe and the folder itself once we exit
    common.notify_icon_change()
    progress(1.0)
    return warnings


# ============================== UI ==============================

class UninstallerApp:
    def __init__(self, root, install_dir, shortcuts):
        self.root = root
        self.install_dir = install_dir
        self.shortcuts = shortcuts
        self.q = queue.Queue()
        self.working = False

        root.title(UNINSTALL_TITLE)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.colors = common.apply_theme(root)   # follows the system's light/dark setting

        self.var_status = tk.StringVar()
        self.page_confirm = self._build_confirm()
        self.page_progress = self._build_progress()
        self.page_done = self._build_done()
        common.size_window(root, 540, (self.page_confirm, self.page_progress, self.page_done), min_height=360)
        self.show(self.page_confirm)

    def _build_confirm(self):
        f = ttk.Frame(self.root, padding=20)
        ttk.Label(f, text=f"Uninstall {common.APP_NAME}", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(f, wraplength=490, justify="left", text="Are you sure you want to uninstall?").pack(anchor="w", pady=(10, 4))
        ttk.Label(f, wraplength=490, justify="left", text="This will remove:").pack(anchor="w", pady=(10, 4))
        lines = [f"- the installation folder at {self.install_dir}", "- the Windows app list entry"]
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
        self.lbl_done.pack(anchor="w", pady=(14, 16))
        # Problems (e.g. a shortcut that couldn't be removed), shown like the installer shows its own
        self.lbl_warn = ttk.Label(f, wraplength=490, justify="left", foreground=self.colors["warning"])
        self.lbl_warn.pack(anchor="w")
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
        def status(text):
            common.log(text)
            self.q.put(("status", text))
        common.log(f"Uninstalling from {self.install_dir}")
        try:
            warnings = uninstall(self.install_dir, status=status,
                                 progress=lambda f: self.q.put(("progress", f)))
            common.log("Uninstalled." + "".join("\n  " + w for w in warnings))
            self.q.put(("done", warnings))
        except UninstallAborted as e:
            common.log(f"Stopped: {e}")
            self.q.put(("aborted", str(e)))
        except Exception as e:
            common.log("Failed:\n" + traceback.format_exc())
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
                    self.working = False
                    # (program files are all removed or the uninstall stops: only shortcuts can be left)
                    if not val:
                        text = "The program was fully removed from your system."
                    else:
                        text = "The program was removed, but the shortcuts below couldn't be. You can delete them yourself."
                    text += "\n\nThe uninstaller will also be gone after you close this window."
                    self.lbl_done.config(text=text)
                    self.lbl_warn.config(text="\n".join("- " + w for w in val))
                    self.show(self.page_done)
                    return
                elif kind == "aborted":
                    # Nothing was changed: back to the confirmation page, to try again later
                    self.working = False
                    self.show(self.page_confirm)
                    common.dialog(self.root, UNINSTALL_TITLE, val)
                    return
                elif kind == "error":
                    self.working = False
                    common.dialog(self.root, UNINSTALL_TITLE, val)
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def on_close(self):
        if not self.working:
            self.root.destroy()


def show_error(text):
    """An error on its own (no uninstaller window behind it), in the system's light/dark theme."""
    root = tk.Tk()
    root.withdraw()
    common.set_window_icon(root)
    common.apply_theme(root)
    common.dialog(root, UNINSTALL_TITLE, text)
    root.destroy()


def main():
    common.start_log("uninstall")
    common.set_dpi_aware()
    d = find_install_dir()
    if not d or not common.is_valid_install_dir(d):
        common.log(f"Not an installation folder: {d}")
        show_error(f"This doesn't look like a {common.APP_NAME} installation folder, so nothing was removed.")
        return
    shortcuts = common.installed_shortcuts(d)
    if common.removal_needs_admin(d, shortcuts) and not common.is_admin():
        # Ask for administrator rights only when this installation needs them to be removed
        common.log("Removing this installation needs administrator rights; asking for them.")
        if not common.relaunch_elevated():
            common.log("Administrator permission wasn't given.")
            show_error(f"Removing this installation of {common.APP_NAME} needs administrator "
                       "permission, which wasn't given. Nothing was removed.")
        return
    root = tk.Tk()
    root.withdraw()   # shown by show_window() once built, title bar already in the right mode
    common.set_window_icon(root)
    UninstallerApp(root, d, shortcuts)
    common.show_window(root)
    root.mainloop()


if __name__ == "__main__":
    main()
    # Exit right away instead of waiting on interpreter shutdown: the self-delete helper is waiting
    # for this process, and a lingering uninstall.exe keeps the folder locked.
    os._exit(0)
