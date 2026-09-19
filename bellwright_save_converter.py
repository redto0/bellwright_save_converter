#!/usr/bin/env python3
r"""Bellwright save converter -- move a co-op world between two Steam accounts.

Bellwright keeps each world under::

    %LOCALAPPDATA%\Bellwright\Saved\SaveGames\<SteamID64>\

Whoever hosts needs the world files sitting in *their own* Steam ID folder, so
handing a campaign back and forth means copying files between two ID-named
folders every session. This automates that.

Important: save files are copied **byte for byte**. Ownership is carried by the
folder name, not by anything inside the file, so there is nothing to rewrite --
and the payload is compressed anyway, so rewriting it would not be safe. Any
version of this tool that edits save bytes is doing the wrong thing.

Run with no arguments for the GUI, or with arguments for the CLI.
Standard library only; no install step.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import struct
from pathlib import Path

# --------------------------------------------------------------------------
# Save container format (reverse engineered)
# --------------------------------------------------------------------------
#
# Offsets below were confirmed against 36 real save files. The file is a fixed
# plaintext header followed by a compressed body:
#
#   0x000  4      magic b"VSWB"
#   0x004  u32    format version (7 in every observed file)
#   0x008  u64    created, unix epoch seconds
#   0x010  u64    last saved, unix epoch seconds (matches file mtime)
#   0x018  u64    progress counter; correlates with playtime but the units are
#                 unproven, so it is reported raw and never labelled "hours"
#   0x020  0x100  character name, NUL-terminated UTF-8
#   0x120  0x100  map name
#   0x220  0x100  user-entered save label
#   0x320  0x100  overlapping string fragments -- deliberately not parsed
#   0x420+ ...    compressed body (entropy ~7.95-7.99 bits/byte)

MAGIC = b"VSWB"
HEADER_SIZE = 0x420

_OFF_VERSION = 0x04
_OFF_CREATED = 0x08
_OFF_SAVED = 0x10
_OFF_COUNTER = 0x18
_OFF_CHARACTER = 0x20
_OFF_MAP = 0x120
_OFF_LABEL = 0x220

_FIELD_SIZE = 0x100

#: SteamID64 = account id + this. Used to convert the 32-bit ids Steam stores
#: in the registry and in ``userdata`` folder names.
STEAM_ID64_BASE = 76561197960265728


def _read_string(buf: bytes, offset: int) -> str:
    """Read one fixed-width NUL-terminated UTF-8 field."""
    raw = buf[offset : offset + _FIELD_SIZE]
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()


def _read_u64(buf: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", buf, offset)[0]


def _to_datetime(epoch: int) -> _dt.datetime | None:
    """Epoch seconds -> local datetime, or None if implausible.

    Local rather than UTC on purpose: it makes the displayed time match the
    file's mtime in Explorer, which is how people sanity-check which save is
    the recent one.
    """
    if not 0 < epoch < (1 << 34):
        return None
    try:
        return _dt.datetime.fromtimestamp(epoch)
    except (OverflowError, OSError, ValueError):
        return None


@dataclasses.dataclass(frozen=True)
class SaveHeader:
    """Metadata read from a save file's header. Purely informational."""

    path: Path
    recognized: bool
    version: int | None = None
    created: _dt.datetime | None = None
    saved: _dt.datetime | None = None
    counter: int | None = None
    character: str = ""
    map_name: str = ""
    label: str = ""

    @property
    def display_label(self) -> str:
        """Label to show in a list; autosaves carry no user label."""
        return self.label or "(autosave)"


def parse_header(path: Path) -> SaveHeader:
    """Read a save file's header.

    Never raises for an unreadable or unexpected file -- a file that fails the
    magic check comes back with ``recognized=False`` and is still copyable.
    Refusing to move a file just because its header looks new would be worse
    than copying it blindly, since copying is byte-exact either way.
    """
    try:
        with path.open("rb") as handle:
            buf = handle.read(HEADER_SIZE)
    except OSError:
        return SaveHeader(path=path, recognized=False)

    if len(buf) < HEADER_SIZE or buf[:4] != MAGIC:
        return SaveHeader(path=path, recognized=False)

    return SaveHeader(
        path=path,
        recognized=True,
        version=struct.unpack_from("<I", buf, _OFF_VERSION)[0],
        created=_to_datetime(_read_u64(buf, _OFF_CREATED)),
        saved=_to_datetime(_read_u64(buf, _OFF_SAVED)),
        counter=_read_u64(buf, _OFF_COUNTER),
        character=_read_string(buf, _OFF_CHARACTER),
        map_name=_read_string(buf, _OFF_MAP),
        label=_read_string(buf, _OFF_LABEL),
    )


# --------------------------------------------------------------------------
# Steam and filesystem discovery
# --------------------------------------------------------------------------

#: Steam's own bookkeeping file. It carries the *account id of whoever owns the
#: folder*, so it must never be copied between folders -- doing so would point
#: the destination folder at the wrong account.
AUTOCLOUD_FILE = "steam_autocloud.vdf"

_DEFAULT_STEAM_PATHS = (
    Path(r"C:\Program Files (x86)\Steam"),
    Path(r"C:\Program Files\Steam"),
)


def is_steam_id64(value: str) -> bool:
    """True for a plausible SteamID64 (17 digits, individual-account range)."""
    return len(value) == 17 and value.isdigit() and value.startswith("7656119")


def account_id_to_steam_id64(account_id: int) -> str:
    return str(account_id + STEAM_ID64_BASE)


def steam_install_path() -> Path | None:
    """Locate the Steam install, preferring the registry over guessing."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            raw, _ = winreg.QueryValueEx(key, "SteamPath")
            candidate = Path(str(raw))
            if candidate.is_dir():
                return candidate
    except (ImportError, OSError):
        pass

    for candidate in _DEFAULT_STEAM_PATHS:
        if candidate.is_dir():
            return candidate
    return None


def _steam_id_from_registry() -> str | None:
    """Currently signed-in user, from Steam's ActiveUser account id."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess"
        ) as key:
            account_id, _ = winreg.QueryValueEx(key, "ActiveUser")
    except (ImportError, OSError):
        return None

    if not account_id:  # zero means Steam is not running
        return None
    candidate = account_id_to_steam_id64(int(account_id))
    return candidate if is_steam_id64(candidate) else None


def _steam_id_from_loginusers() -> str | None:
    """Last auto-login account, from Steam's loginusers.vdf."""
    import re

    steam_path = steam_install_path()
    if steam_path is None:
        return None
    vdf = steam_path / "config" / "loginusers.vdf"
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    blocks = re.findall(r'"(\d{17})"\s*\{(.*?)\}', text, re.DOTALL)
    for steam_id, body in blocks:
        if re.search(r'"AutoLogin"\s*"1"', body) and is_steam_id64(steam_id):
            return steam_id
    # No explicit auto-login flag: only safe to guess when there is one account.
    if len(blocks) == 1 and is_steam_id64(blocks[0][0]):
        return blocks[0][0]
    return None


def savegames_root() -> Path:
    r"""``%LOCALAPPDATA%\Bellwright\Saved\SaveGames``."""
    import os

    local_appdata = os.environ.get("LOCALAPPDATA")
    base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
    return base / "Bellwright" / "Saved" / "SaveGames"


def _steam_id_from_savegames() -> str | None:
    """Fall back to the save folder itself when exactly one account has played."""
    root = savegames_root()
    if not root.is_dir():
        return None
    folders = [d.name for d in root.iterdir() if d.is_dir() and is_steam_id64(d.name)]
    return folders[0] if len(folders) == 1 else None


class DetectionError(RuntimeError):
    """Raised when the local Steam account cannot be determined."""


def detect_local_steam_id() -> str:
    """Determine this machine's Steam account, trying the most reliable first.

    Nothing is hardcoded: the registry knows who is signed in right now, the
    login file knows who signed in last, and the save folder knows who has
    actually played. Any one of them is enough.
    """
    for source in (
        _steam_id_from_registry,
        _steam_id_from_loginusers,
        _steam_id_from_savegames,
    ):
        found = source()
        if found:
            return found
    raise DetectionError(
        "Could not determine your Steam ID automatically. "
        "Pass --steam-id <17-digit id> explicitly."
    )


def local_save_folder(steam_id: str | None = None) -> Path:
    """The folder this machine's Bellwright reads and writes."""
    return savegames_root() / (steam_id or detect_local_steam_id())


def find_candidate_sources() -> list[Path]:
    r"""Look for ``<drive>:\<SteamID64>`` folders, e.g. on a handed-over USB stick.

    Used to prefill the GUI's source box so the common case needs no typing.
    """
    import string

    found: list[Path] = []
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:\\")
        try:
            if not drive.is_dir():
                continue
            for child in drive.iterdir():
                if child.is_dir() and is_steam_id64(child.name):
                    found.append(child)
        except OSError:
            continue  # unreadable or empty drive
    return found


# --------------------------------------------------------------------------
# Save sets
# --------------------------------------------------------------------------
#
# A "save set" is every file belonging to one character: the numbered slots,
# the autosaves, the quicksave, and their .sav_backup0 fallbacks. Bellwright
# names them "<Character>_<slot>.sav", so the character is the token before the
# first underscore.


def save_set_name(filename: str) -> str:
    """Character name for a save file, from its filename."""
    head = filename.split("_", 1)[0]
    return head.split(".", 1)[0]


def is_save_file(path: Path) -> bool:
    """True for ``.sav`` and ``.sav_backup0``; excludes Steam's own files."""
    name = path.name.lower()
    if name == AUTOCLOUD_FILE.lower() or ".bak-" in name:
        return False
    return ".sav" in name


@dataclasses.dataclass
class SaveSet:
    """Every file belonging to one character, plus metadata for display."""

    name: str
    files: list[Path] = dataclasses.field(default_factory=list)
    header: SaveHeader | None = None  # from the most recently saved file

    @property
    def total_bytes(self) -> int:
        total = 0
        for path in self.files:
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return total

    @property
    def saved(self) -> _dt.datetime | None:
        return self.header.saved if self.header else None

    @property
    def character(self) -> str:
        return self.header.character if self.header else self.name

    @property
    def map_name(self) -> str:
        return self.header.map_name if self.header else ""

    @property
    def label(self) -> str:
        return self.header.display_label if self.header else ""

    @property
    def name_matches_header(self) -> bool:
        """Filename and in-file character name agree.

        A mismatch means a renamed file, which is worth surfacing but not worth
        blocking a copy over.
        """
        if self.header is None or not self.header.character:
            return True
        return self.header.character.lower() == self.name.lower()


def scan_folder(folder: Path) -> list[SaveSet]:
    """Group a save folder into per-character sets, newest first.

    Grouping compares the pre-underscore token *exactly*. Prefix matching would
    be wrong here: a folder can hold both "SirRed" and "SirRedDevil1", and
    startswith() would fold the second into the first.
    """
    if not folder.is_dir():
        return []

    groups: dict[str, SaveSet] = {}
    for path in sorted(folder.iterdir()):
        if not path.is_file() or not is_save_file(path):
            continue
        name = save_set_name(path.name)
        # Windows filenames are case-insensitive; key on case, display as found.
        save_set = groups.setdefault(name.lower(), SaveSet(name=name))
        save_set.files.append(path)

    for save_set in groups.values():
        # Describe the set by its most recent save, which is the one a person
        # is actually looking for.
        headers = [parse_header(p) for p in save_set.files]
        recognized = [h for h in headers if h.recognized and h.saved]
        if recognized:
            save_set.header = max(recognized, key=lambda h: h.saved)
        elif headers:
            save_set.header = headers[0]

    return sorted(
        groups.values(),
        key=lambda s: (s.saved is not None, s.saved or _dt.datetime.min),
        reverse=True,
    )


# --------------------------------------------------------------------------
# Transfer engine
# --------------------------------------------------------------------------


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
