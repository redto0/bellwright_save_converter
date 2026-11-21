"""Moving saves between Steam ID folders: import, export and snapshots."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
from pathlib import Path

from .saves import AUTOCLOUD_FILE, is_save_file, scan_folder
from .steam import savegames_root


class TransferError(RuntimeError):
    """A transfer could not be completed safely."""


def sha256(path: Path, _chunk: int = 1 << 20) -> str:
    """Hash a file so a copy can be proven identical to its source."""
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def game_is_running() -> bool:
    """True if Bellwright looks like it is open.

    Copying under a running game risks catching a half-written save, which is
    why the manual procedure starts by closing it.
    """
    import subprocess

    try:
        # Plain tasklist rather than /FI: the filter's wildcard semantics vary,
        # and a substring scan over the full list is unambiguous. The real
        # process names are BellwrightGame.exe and BellwrightGame-Win64-Shipping.
        out = subprocess.run(
            ["tasklist"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False  # can't tell; don't block the user over it
    return "bellwright" in out.lower()


def _timestamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def backups_root() -> Path:
    """Where snapshots live -- beside the save folder, never inside it.

    Kept out of SaveGames so the game never sees stray files.
    """
    return savegames_root().parent.parent / "SaveBackups"


@dataclasses.dataclass
class PlannedCopy:
    """One file that an import would copy."""

    source: Path
    dest: Path

    @property
    def collides(self) -> bool:
        return self.dest.exists()

    @property
    def size(self) -> int:
        try:
            return self.source.stat().st_size
        except OSError:
            return 0


def plan_import(
    source: Path,
    dest: Path,
    names: list[str] | None = None,
    include_backups: bool = True,
) -> list[PlannedCopy]:
    """Work out exactly which files an import would write.

    ``names`` selects save sets by character name; None means all of them.
    Nothing is written here, so the result can be shown for confirmation.
    """
    sets = scan_folder(source)
    if names:
        wanted = {n.lower() for n in names}
        known = {s.name.lower() for s in sets}
        missing = wanted - known
        if missing:
            raise TransferError(
                f"No save set named {', '.join(sorted(missing))} in {source}. "
                f"Available: {', '.join(s.name for s in sets) or '(none)'}"
            )
        sets = [s for s in sets if s.name.lower() in wanted]

    planned: list[PlannedCopy] = []
    for save_set in sets:
        for path in save_set.files:
            if not include_backups and "_backup" in path.name.lower():
                continue
            # AUTOCLOUD_FILE is already excluded by is_save_file(); this is the
            # belt-and-braces check, because copying it would repoint the
            # destination folder at the source's Steam account.
            if path.name.lower() == AUTOCLOUD_FILE.lower():
                continue
            planned.append(PlannedCopy(source=path, dest=dest / path.name))
    return planned


def execute_import(
    planned: list[PlannedCopy],
    dry_run: bool = False,
    log=print,
) -> dict[str, object]:
    """Copy the planned files, verifying every one.

    Existing files are moved into a timestamped snapshot folder rather than
    being overwritten, so a mistaken import is always undoable.
    """
    import shutil

    if not planned:
        raise TransferError("Nothing to import.")

    dest_dir = planned[0].dest.parent
    collisions = [p for p in planned if p.collides]
    total = sum(p.size for p in planned)

    log(f"{len(planned)} file(s), {total / 1e6:.1f} MB -> {dest_dir}")
    if collisions:
        log(f"{len(collisions)} existing file(s) will be moved to a backup first.")

    if dry_run:
        for p in planned:
            log(f"  [dry-run] {p.source.name}{'  (replaces existing)' if p.collides else ''}")
        return {"copied": 0, "dry_run": True, "planned": len(planned), "backup": None}

    dest_dir.mkdir(parents=True, exist_ok=True)

    backup_dir: Path | None = None
    if collisions:
        backup_dir = backups_root() / f"pre-import-{_timestamp()}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for p in collisions:
            shutil.move(str(p.dest), str(backup_dir / p.dest.name))
        log(f"Moved {len(collisions)} existing file(s) to {backup_dir}")

    copied: list[Path] = []
    try:
        for p in planned:
            shutil.copy2(p.source, p.dest)
            if sha256(p.source) != sha256(p.dest):
                raise TransferError(f"Verification failed for {p.dest.name}")
            copied.append(p.dest)
            log(f"  ok  {p.source.name}  ({p.size / 1e6:.1f} MB)")
    except Exception:
        # Roll back: remove what this run wrote, then put back what it moved.
        for path in copied:
            path.unlink(missing_ok=True)
        if backup_dir is not None:
            for path in backup_dir.iterdir():
                shutil.move(str(path), str(dest_dir / path.name))
            log("Rolled back; your original files are restored.")
        raise

    log(f"Imported {len(copied)} file(s), all SHA-256 verified.")
    return {
        "copied": len(copied),
        "dry_run": False,
        "planned": len(planned),
        "backup": str(backup_dir) if backup_dir else None,
    }


def _safe_zip_members(archive) -> list[str]:
    """Flat filenames only.

    The zips this tool writes are flat by design, so anything carrying a path
    separator, a drive, or a parent reference did not come from here and is
    rejected rather than extracted somewhere unexpected.
    """
    names = []
    for name in archive.namelist():
        if name.endswith("/"):
            continue
        if "/" in name or "\\" in name or Path(name).is_absolute() or ".." in name:
            raise TransferError(f"Refusing to extract unsafe archive entry: {name!r}")
        names.append(name)
    return names


def export_zip(
    source: Path,
    dest_zip: Path,
    names: list[str] | None = None,
    include_backups: bool = True,
    log=print,
) -> Path:
    """Zip up save sets to hand to the other player.

    The archive is flat -- just the save files -- so the person receiving it can
    import it without caring which Steam ID folder it came out of.
    """
    import zipfile

    planned = plan_import(source, Path("."), names=names, include_backups=include_backups)
    if not planned:
        raise TransferError(f"No save files to export from {source}")

    dest_zip = dest_zip.with_suffix(".zip")
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in planned:
            archive.write(item.source, arcname=item.source.name)
            log(f"  + {item.source.name}")
    log(f"Wrote {dest_zip} ({dest_zip.stat().st_size / 1e6:.1f} MB)")
    return dest_zip


def snapshot(folder: Path, tag: str = "manual", log=print) -> Path | None:
    """Zip a save folder so the current state can be restored later."""
    import zipfile

    if not folder.is_dir():
        return None
    files = [p for p in sorted(folder.iterdir()) if p.is_file() and is_save_file(p)]
    if not files:
        log(f"Nothing to back up in {folder}")
        return None

    dest = backups_root() / f"{tag}-{_timestamp()}.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, arcname=path.name)
    log(f"Backed up {len(files)} file(s) to {dest}")
    return dest


def list_snapshots() -> list[Path]:
    """Existing snapshots, newest first."""
    root = backups_root()
    if not root.is_dir():
        return []
    return sorted(root.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)


@contextlib.contextmanager
def resolve_source(source: Path):
    """Yield a folder of save files, unpacking a .zip first if needed.

    Lets import accept either a folder handed over on a USB stick or a zip
    produced by export, which is what makes the swap symmetric.
    """
    import shutil
    import tempfile
    import zipfile

    if source.is_dir():
        yield source
        return
    if source.suffix.lower() != ".zip" or not source.is_file():
        raise TransferError(f"Not a save folder or .zip archive: {source}")

    tmp = Path(tempfile.mkdtemp(prefix="bsc-"))
    try:
        with zipfile.ZipFile(source) as archive:
            for name in _safe_zip_members(archive):
                with archive.open(name) as src, (tmp / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
