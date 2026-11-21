"""Tkinter window over the same engine the CLI uses.

Deliberately plain: the person on the other end of a swap may not be the one
who set this up, so every action that writes states what it will do first,
and the window always shows which Steam account is receiving the files.
"""

from __future__ import annotations

from pathlib import Path

from .oodle import Oodle, find_oodle
from .respawn import RESPAWN_TESTED_VERSION, ore_by_metal, respawn_nodes, write_respawned
from .saves import SaveHeader, SaveSet, parse_header, scan_folder
from .steam import DetectionError, detect_local_steam_id, find_candidate_sources, local_save_folder
from .transfer import (
    TransferError,
    backups_root,
    execute_import,
    export_zip,
    game_is_running,
    list_snapshots,
    plan_import,
    resolve_source,
    snapshot,
)


class _App:
    """Tkinter front end over the same engine the CLI uses."""

    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk

        self.root = root
        self.sets: list[SaveSet] = []
        self.source: Path | None = None
        self._busy = False

        root.title("Bellwright Save Converter")
        root.geometry("920x580")
        root.minsize(760, 470)

        try:
            self.steam_id: str | None = detect_local_steam_id()
            self.dest: Path | None = local_save_folder(self.steam_id)
            banner = f"Importing into your account:  {self.steam_id}"
        except DetectionError as exc:
            self.steam_id = None
            self.dest = None
            banner = f"Steam account not detected — {exc}"

        header = ttk.Frame(root, padding=(12, 10, 12, 4))
        header.pack(fill="x")
        ttk.Label(header, text=banner, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        if self.dest is not None:
            ttk.Label(header, text=str(self.dest), foreground="#555555").pack(anchor="w")

        picker = ttk.Frame(root, padding=(12, 6))
        picker.pack(fill="x")
        ttk.Label(picker, text="Source:").pack(side="left")
        self.source_var = tk.StringVar()
        ttk.Entry(picker, textvariable=self.source_var).pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Button(picker, text="Folder...", command=self._pick_folder).pack(side="left")
        ttk.Button(picker, text="Zip...", command=self._pick_zip).pack(side="left", padx=4)
        ttk.Button(picker, text="Refresh", command=self.refresh).pack(side="left")

        columns = ("files", "size", "label", "map", "saved")
        self.tree = ttk.Treeview(root, columns=columns, show="tree headings", height=9)
        self.tree.heading("#0", text="Save")
        self.tree.column("#0", width=170, anchor="w")
        for key, title, width in (
            ("files", "Files", 60),
            ("size", "Size", 90),
            ("label", "Label", 190),
            ("map", "Map", 130),
            ("saved", "Last saved", 150),
        ):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=12, pady=6)

        buttons = ttk.Frame(root, padding=(12, 0, 12, 8))
        buttons.pack(fill="x")
        self.include_backups = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            buttons,
            text="Include .sav_backup0 fallbacks",
            variable=self.include_backups,
        ).pack(side="left")
        ttk.Button(buttons, text="Restore backup", command=self.on_restore).pack(side="right")
        ttk.Button(buttons, text="Respawn ores...", command=self.on_respawn).pack(
            side="right", padx=4
        )
        ttk.Button(buttons, text="Back up mine", command=self.on_backup).pack(
            side="right", padx=4
        )
        ttk.Button(buttons, text="Export selected...", command=self.on_export).pack(side="right")
        ttk.Button(buttons, text="Import selected", command=self.on_import).pack(
            side="right", padx=6
        )

        self.log_box = tk.Text(root, height=8, wrap="word", state="disabled")
        self.log_box.pack(fill="x", padx=12, pady=(0, 12))

        candidates = find_candidate_sources()
        if candidates:
            self.source_var.set(str(candidates[0]))
            self.refresh()
        else:
            self.log("Choose the folder or zip holding the saves you were given.")

    # -- helpers ---------------------------------------------------------

    def log(self, *parts) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", " ".join(str(p) for p in parts) + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        self.root.update_idletasks()

    def _pick_folder(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(title="Folder containing the saves")
        if chosen:
            self.source_var.set(chosen)
            self.refresh()

    def _pick_zip(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askopenfilename(
            title="Save archive", filetypes=[("Zip archives", "*.zip")]
        )
        if chosen:
            self.source_var.set(chosen)
            self.refresh()

    def selected_names(self) -> list[str]:
        """Selected save sets, or all of them when nothing is selected."""
        picked = [self.tree.item(item, "text") for item in self.tree.selection()]
        return picked or [s.name for s in self.sets]

    def _guard(self) -> bool:
        """Make sure the game is closed before touching files."""
        from tkinter import messagebox

        if game_is_running():
            return messagebox.askyesno(
                "Bellwright is running",
                "Bellwright appears to be open, so a save could be mid-write.\n\n"
                "Close the game first. Continue anyway?",
                icon="warning",
            )
        return True

    # -- actions ---------------------------------------------------------

    def refresh(self) -> None:
        from tkinter import messagebox

        self.tree.delete(*self.tree.get_children())
        self.sets = []
        raw = self.source_var.get().strip().strip('"')
        if not raw:
            return

        source = Path(raw)
        try:
            with resolve_source(source) as folder:
                self.sets = scan_folder(folder)
        except (TransferError, OSError) as exc:
            messagebox.showerror("Cannot read source", str(exc))
            return

        self.source = source
        for item in self.sets:
            when = item.saved.strftime("%Y-%m-%d %H:%M") if item.saved else "unknown"
            self.tree.insert(
                "",
                "end",
                text=item.name,
                values=(
                    len(item.files),
                    f"{item.total_bytes / 1e6:.1f} MB",
                    item.label,
                    item.map_name,
                    when,
                ),
            )
        self.log(f"Found {len(self.sets)} save set(s) in {source}")
        if self.sets:
            self.log("Pick one and press Import, or import everything by selecting nothing.")

    def on_import(self) -> None:
        from tkinter import messagebox

        if not self.sets or self.dest is None or self.source is None:
            messagebox.showinfo("Nothing to import", "Choose a source folder or zip first.")
            return
        if not self._guard():
            return

        names = self.selected_names()
        try:
            with resolve_source(self.source) as folder:
                planned = plan_import(
                    folder,
                    self.dest,
                    names=names,
                    include_backups=self.include_backups.get(),
                )
                total = sum(p.size for p in planned)
                replacing = sum(1 for p in planned if p.collides)
                message = (
                    f"Import {len(planned)} file(s), {total / 1e6:.1f} MB\n\n"
                    f"from:  {self.source}\n"
                    f"into:  {self.dest}\n\n"
                    f"Save sets: {', '.join(names)}"
                )
                if replacing:
                    message += (
                        f"\n\n{replacing} existing file(s) will be backed up first, "
                        "not overwritten."
                    )
                if not messagebox.askokcancel("Confirm import", message):
                    return
                snapshot(self.dest, tag="pre-import", log=self.log)
                execute_import(planned, log=self.log)
        except (TransferError, OSError) as exc:
            messagebox.showerror("Import failed", str(exc))
            return
        messagebox.showinfo("Done", "Import complete. Launch Bellwright and load the save.")

    def on_export(self) -> None:
        from tkinter import filedialog, messagebox

        if not self.sets or self.source is None:
            messagebox.showinfo("Nothing to export", "Choose a source folder first.")
            return

        names = self.selected_names()
        suggested = (
            f"bellwright-{names[0]}.zip" if len(names) == 1 else "bellwright-saves.zip"
        )
        target = filedialog.asksaveasfilename(
            title="Save archive as",
            defaultextension=".zip",
            initialfile=suggested,
            filetypes=[("Zip archives", "*.zip")],
        )
        if not target:
            return
        try:
            with resolve_source(self.source) as folder:
                export_zip(
                    folder,
                    Path(target),
                    names=names,
                    include_backups=self.include_backups.get(),
                    log=self.log,
                )
        except (TransferError, OSError) as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        messagebox.showinfo("Done", f"Wrote {target}")

    def on_backup(self) -> None:
        from tkinter import messagebox

        if self.dest is None:
            return
        made = snapshot(self.dest, tag="manual", log=self.log)
        messagebox.showinfo(
            "Backup",
            f"Saved to {made}" if made else "You have no saves to back up yet.",
        )

    def on_restore(self) -> None:
        from tkinter import filedialog, messagebox

        if self.dest is None:
            return
        snapshots = list_snapshots()
        root_dir = backups_root()
        chosen = filedialog.askopenfilename(
            title="Choose a snapshot",
            initialdir=str(root_dir) if root_dir.is_dir() else None,
            initialfile=snapshots[0].name if snapshots else "",
            filetypes=[("Zip archives", "*.zip")],
        )
        if not chosen or not self._guard():
            return
        try:
            with resolve_source(Path(chosen)) as folder:
                execute_import(plan_import(folder, self.dest), log=self.log)
        except (TransferError, OSError) as exc:
            messagebox.showerror("Restore failed", str(exc))
            return
        messagebox.showinfo("Done", "Restore complete.")

    def on_respawn(self) -> None:
        from tkinter import filedialog, messagebox

        if self.dest is None or self._busy:
            return
        chosen = filedialog.askopenfilename(
            title="Choose the save to reset ore in",
            initialdir=str(self.dest),
            filetypes=[("Bellwright saves", "*.sav")],
        )
        if not chosen:
            return
        target = Path(chosen)

        # Stricter than imports: with the game open, its next autosave would
        # just undo the reset, so there is no "continue anyway".
        if game_is_running():
            messagebox.showerror(
                "Close Bellwright first",
                "Bellwright is open. Close the game, then try again.",
            )
            return
        header = parse_header(target)
        if not header.recognized:
            messagebox.showerror("Not a save", f"{target.name} is not a Bellwright save.")
            return
        if header.version != RESPAWN_TESTED_VERSION:
            messagebox.showerror(
                "Untested save format",
                f"{target.name} is format v{header.version}, and ore reset has only been "
                f"tested on v{RESPAWN_TESTED_VERSION}. Use the command line with --out "
                "to try it on a copy.",
            )
            return

        self.log(f"Reading {target.name} ({header.display_label})...")

        def prepare(log):
            oodle = Oodle(find_oodle(log=log))
            new_raw, removed = respawn_nodes(target.read_bytes(), oodle)
            return oodle, new_raw, removed

        self._in_background(
            prepare, lambda result, error: self._respawn_review(target, header, result, error)
        )

    def _in_background(self, job, done) -> None:
        """Run job(log) on a worker thread, then done(result, error) on the UI thread.

        Finding Oodle and repacking a save can take half a minute, which would
        freeze the window. The worker never touches Tk: its log lines and its
        outcome go through queues that the UI thread polls.
        """
        import queue
        import threading

        messages: queue.Queue = queue.Queue()
        outcome: queue.Queue = queue.Queue()

        def log_from_worker(*parts):
            messages.put(" ".join(str(p) for p in parts))

        def work():
            try:
                outcome.put((job(log_from_worker), None))
            except Exception as exc:  # handed to done() on the UI thread
                outcome.put((None, exc))

        def drain():
            while not messages.empty():
                self.log(messages.get_nowait())

        def poll():
            drain()
            try:
                result, error = outcome.get_nowait()
            except queue.Empty:
                self.root.after(100, poll)
                return
            drain()
            self._set_busy(False)
            done(result, error)

        self._set_busy(True)
        threading.Thread(target=work, daemon=True).start()
        self.root.after(100, poll)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.root.configure(cursor="watch" if busy else "")

    def _respawn_review(self, target: Path, header: SaveHeader, result, error) -> None:
        from tkinter import messagebox

        if error is not None:
            self.log(f"Ore reset failed: {error}")
            messagebox.showerror("Ore reset failed", str(error))
            return

        oodle, new_raw, removed = result
        total = sum(removed.values())
        if not total:
            self.log(f"{target.name}: 0 harvested ore nodes recorded; nothing to reset.")
            messagebox.showinfo(
                "Nothing to reset", f"No harvested ore is recorded in {target.name}."
            )
            return

        metals = ore_by_metal(removed)
        breakdown = ", ".join(f"{metal} {count}" for metal, count in metals.items())
        self.log(f"{target.name}: {total} harvested ore nodes affected ({breakdown}).")
        lines = "\n".join(f"{metal}:  {count}" for metal, count in metals.items())
        if not messagebox.askokcancel(
            "Confirm ore reset",
            f"Reset {total} harvested ore nodes in\n"
            f"{target.name}  ({header.display_label})?\n\n{lines}\n\n"
            "Your save folder is backed up first.",
        ):
            self.log("Ore reset cancelled; nothing written.")
            return
        if game_is_running():  # it may have been started while we worked
            messagebox.showerror(
                "Close Bellwright first", "Bellwright was started; nothing was written."
            )
            return

        def commit(log):
            return write_respawned(target, new_raw, oodle, log=log)

        def finished(left, error):
            self._respawn_finished(target, header, total, breakdown, left, error)

        self._in_background(commit, finished)

    def _respawn_finished(self, target: Path, header: SaveHeader, total: int,
                          breakdown: str, left, error) -> None:
        from tkinter import messagebox

        if error is not None:
            self.log(f"Ore reset failed: {error}")
            messagebox.showerror("Ore reset failed", str(error))
            return
        self.log(f"Checked {target.name} on disk: {total} nodes reset, "
                 f"{left} harvested ore records left.")
        if left:
            messagebox.showerror(
                "Check failed",
                f"{left} harvested ore records are still in {target.name}. "
                "Restore the pre-respawn backup.",
            )
            return
        messagebox.showinfo(
            "Done",
            f"Reset {total} ore nodes in {target.name} ({breakdown}).\n"
            f"Checked on disk: 0 left.\n\nLoad '{header.display_label}' in game.",
        )


def run_gui() -> int:
    """Open the window, falling back to CLI help if Tkinter is missing."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        from .cli import build_parser

        print("Tkinter is not available; use the command line instead.\n")
        build_parser().print_help()
        return 1

    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass  # any default theme is fine
    _App(root)
    root.mainloop()
    return 0
