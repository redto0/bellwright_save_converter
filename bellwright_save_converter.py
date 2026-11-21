#!/usr/bin/env python3
r"""Bellwright save converter -- move a co-op world between two Steam accounts.

Bellwright keeps each world under::

    %LOCALAPPDATA%\Bellwright\Saved\SaveGames\<SteamID64>\

Whoever hosts needs the world files sitting in *their own* Steam ID folder, so
handing a campaign back and forth means copying files between two ID-named
folders every session. This automates that.

Important: transfers copy save files **byte for byte**. Ownership is carried by
the folder name, not by anything inside the file, so moving a world between
accounts never needs to rewrite it.

The one command that does edit a save is ``respawn``, which resets harvested
ore nodes. It is opt-in, snapshots the folder first, and proves its output
round-trips before writing anything.

Run with no arguments for the GUI, or with arguments for the CLI.
Standard library only. ``respawn`` also needs Oodle's oo2core DLL, which it
finds in other installed games.
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
# Offsets below were confirmed against real save files. The file is a fixed
# plaintext header followed by a compressed body:
#
#   0x000  4      magic b"VSWB"
#   0x004  u32    format version (7 before the mid-2026 patch, 8 after; the
#                 game picks its deserializer from this, so it is not cosmetic)
#   0x008  u64    created, unix epoch seconds
#   0x010  u64    last saved, unix epoch seconds (matches file mtime)
#   0x018  u64    uncompressed body size; the engine logs it as the BulkData
#                 "Read Size", so it must match the body exactly
#   0x020  0x100  character name, NUL-terminated UTF-8
#   0x120  0x100  map name
#   0x220  0x100  user-entered save label
#   0x320  0x100  settlement name, plus leftover string fragments
#   0x420  f64    world clock, in-game seconds
#   ...           UE5 compressed archive, located by ARCHIVE_TAG (see the
#                 harvest node section below)

MAGIC = b"VSWB"
HEADER_SIZE = 0x420

_OFF_VERSION = 0x04
_OFF_CREATED = 0x08
_OFF_SAVED = 0x10
_OFF_BODY_SIZE = 0x18
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
    body_size: int | None = None
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
        body_size=_read_u64(buf, _OFF_BODY_SIZE),
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


# --------------------------------------------------------------------------
# Harvest node respawn
# --------------------------------------------------------------------------
#
# The one feature that edits a save. After the header the file holds a UE5
# compressed archive: an ARCHIVE_TAG header, a chunk table, then 128 KiB chunks
# compressed with Oodle Kraken. Decompressed, the world is protobuf.
#
# Every foliage instance the player has touched -- an ore chunk picked up, a
# deposit mined -- has one MistFoliageDataComponent record, keyed by (mesh type,
# instance id). Untouched foliage has no record at all, so deleting a node's
# record puts it back to its pristine, harvestable state. Verified in game for
# the ore types below on format version 8.

ARCHIVE_TAG = 0x222222229E2A83C1
FOLIAGE_DATA_CLASS = "/Script/Mist.MistFoliageDataComponent"

#: Copper and tin surface pickups, plus copper, iron and tin rock deposits.
#: Matched exactly: a loose "ore" pattern would also hit every "Forest" mesh.
ORE_MESH_PATTERN = r"SM_(CopperOre_\d+|Tin|MEDIUM_(Copper|Iron|Tin)Rock_\d+)"

RESPAWN_TESTED_VERSION = 8

_OODLE_KRAKEN = 8
_OODLE_LEVEL_NORMAL = 4

# Body layout as protobuf field numbers: 8 is the global string table and 21
# the world. Inside the world, 2 > 2 are record containers holding records in 2.
_BODY_STRINGS = 8
_BODY_WORLD = 21
_RECORD_CLASS = 5
_RECORD_IDENTITY = 4

_DROP = object()


class OodleError(TransferError):
    """The Oodle library could not be found or used."""


def _steam_library_paths() -> list[Path]:
    """Every Steam library folder listed in libraryfolders.vdf."""
    import re

    steam = steam_install_path()
    if steam is None:
        return []
    libraries = [steam]
    try:
        text = (steam / "steamapps" / "libraryfolders.vdf").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return libraries
    for raw in re.findall(r'"path"\s*"([^"]+)"', text):
        path = Path(raw.replace("\\\\", "\\"))
        if path.is_dir() and path not in libraries:
            libraries.append(path)
    return libraries


def _oodle_version(path: Path) -> int:
    import re

    match = re.search(r"oo2core_(\d+)_win64", path.name, re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _is_oodle_dll(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("oo2core_") and lowered.endswith("_win64.dll")


def find_oodle(explicit: str | None = None, log=print) -> Path:
    r"""Locate an oo2core_*_win64.dll to decompress saves with.

    Bellwright compiles Oodle into its executable, so there is no DLL to borrow
    from the game itself. Plenty of other games ship it standalone (Warframe
    under Tools\Oodle, Destiny 2, ...) and any of them will do -- the format
    does not depend on which game the library came from.

    Search order: --oodle, the BELLWRIGHT_OODLE environment variable, next to
    this script, then every Steam library.
    """
    import os

    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise OodleError(f"No such file: {path}")
        return path

    env = os.environ.get("BELLWRIGHT_OODLE")
    if env and Path(env).is_file():
        return Path(env)

    here = Path(__file__).resolve().parent
    beside = [p for p in here.iterdir() if p.is_file() and _is_oodle_dll(p.name)]
    if beside:
        return max(beside, key=_oodle_version)

    log("Searching Steam libraries for an Oodle DLL...")
    found: list[Path] = []
    for library in _steam_library_paths():
        common = library / "steamapps" / "common"
        if not common.is_dir():
            continue
        base_depth = len(common.parts)
        for dirpath, dirnames, filenames in os.walk(common):
            if len(Path(dirpath).parts) - base_depth >= 6:
                dirnames[:] = []
            found.extend(Path(dirpath) / name for name in filenames if _is_oodle_dll(name))
    if not found:
        raise OodleError(
            "No oo2core_*_win64.dll found. Copy one from any game that ships it "
            "next to this script, or pass --oodle <path>."
        )
    best = max(found, key=_oodle_version)
    log(f"Using {best}  (copy it next to this script to skip the search)")
    return best


class Oodle:
    """The two Oodle calls the save format needs, over ctypes."""

    def __init__(self, dll: Path):
        import ctypes

        if struct.calcsize("P") != 8:
            raise OodleError("Oodle's win64 DLL needs 64-bit Python.")
        try:
            lib = ctypes.WinDLL(str(dll))
        except OSError as exc:
            raise OodleError(f"Could not load {dll}: {exc}") from exc

        size, ptr, num = ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_int
        lib.OodleLZ_Decompress.restype = size
        lib.OodleLZ_Decompress.argtypes = [
            ptr, size, ptr, size, num, num, num, ptr, size, ptr, ptr, ptr, size, num,
        ]
        lib.OodleLZ_Compress.restype = size
        lib.OodleLZ_Compress.argtypes = [num, ptr, size, ptr, num, ptr, ptr, ptr, ptr, size]
        self._ctypes = ctypes
        self._lib = lib

    def decompress(self, src: bytes, raw_len: int) -> bytes:
        dst = self._ctypes.create_string_buffer(raw_len)
        # fuzz-safe on, no CRC check, quiet, decode all thread phases
        got = self._lib.OodleLZ_Decompress(
            src, len(src), dst, raw_len, 1, 0, 0, None, 0, None, None, None, 0, 3
        )
        if got != raw_len:
            raise OodleError(f"Oodle decompressed {got} bytes, expected {raw_len}")
        return dst.raw

    def compress(self, raw: bytes) -> bytes:
        dst = self._ctypes.create_string_buffer(len(raw) + 65536)
        got = self._lib.OodleLZ_Compress(
            _OODLE_KRAKEN, raw, len(raw), dst, _OODLE_LEVEL_NORMAL, None, None, None, None, 0
        )
        if got <= 0:
            raise OodleError("Oodle compression failed")
        return dst.raw[:got]


@dataclasses.dataclass
class _Archive:
    offset: int                      # where ARCHIVE_TAG starts
    chunk_size: int
    compressor: int
    body_size: int
    chunks: list[tuple[int, int]]    # (compressed, uncompressed) per chunk
    data_offset: int


def _parse_archive(raw: bytes) -> _Archive:
    offset = raw.find(struct.pack("<Q", ARCHIVE_TAG))
    if offset < 0:
        raise TransferError("No compressed archive found in this save.")
    o = offset + 8
    (chunk_size,) = struct.unpack_from("<q", raw, o)
    compressor = raw[o + 8]
    _compressed_total, body_size = struct.unpack_from("<qq", raw, o + 9)
    o += 25
    count = -(-body_size // chunk_size)
    chunks = [struct.unpack_from("<qq", raw, o + 16 * i) for i in range(count)]
    return _Archive(offset, chunk_size, compressor, body_size, chunks, o + 16 * count)


def unpack_body(raw: bytes, oodle: Oodle) -> bytes:
    """Decompress a save's protobuf body."""
    archive = _parse_archive(raw)
    out = bytearray()
    o = archive.data_offset
    for compressed, uncompressed in archive.chunks:
        out += oodle.decompress(raw[o:o + compressed], uncompressed)
        o += compressed
    if len(out) != archive.body_size:
        raise TransferError("Decompressed size does not match the archive header.")
    return bytes(out)


def pack_body(original: bytes, body: bytes, oodle: Oodle) -> bytes:
    """Rebuild a save around a new body, keeping the original header."""
    archive = _parse_archive(original)
    size = archive.chunk_size
    pieces = [oodle.compress(body[i:i + size]) for i in range(0, len(body), size)]
    table = b"".join(
        struct.pack("<qq", len(piece), min(size, len(body) - i * size))
        for i, piece in enumerate(pieces)
    )
    header = bytearray(original[:archive.offset])
    # The engine sizes its read from this field; it must match the new body.
    struct.pack_into("<Q", header, _OFF_BODY_SIZE, len(body))
    return (
        bytes(header)
        + struct.pack("<Qq", ARCHIVE_TAG, size)
        + bytes([archive.compressor])
        + struct.pack("<qq", sum(len(p) for p in pieces), len(body))
        + table
        + b"".join(pieces)
    )


def _pb_varint(buf: bytes, o: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[o]
        o += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, o
        shift += 7


def _pb_encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _pb_fields(buf: bytes, start: int, end: int):
    """Yield (field, wire_type, key_start, value_start, value_end, int_value)."""
    o = start
    while o < end:
        key_start = o
        key, o = _pb_varint(buf, o)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, nxt = _pb_varint(buf, o)
            yield field, wire, key_start, o, nxt, value
            o = nxt
        elif wire == 1:
            yield field, wire, key_start, o, o + 8, None
            o += 8
        elif wire == 5:
            yield field, wire, key_start, o, o + 4, None
            o += 4
        elif wire == 2:
            length, o = _pb_varint(buf, o)
            if o + length > end:
                raise TransferError("Corrupt save body: a field overruns its message.")
            yield field, wire, key_start, o, o + length, length
            o += length
        else:
            raise TransferError(f"Corrupt save body: unknown wire type {wire}.")
    if o != end:
        raise TransferError("Corrupt save body: misaligned message.")


def _pb_spans(buf: bytes, start: int, end: int, field: int) -> list[tuple[int, int]]:
    """Value ranges of every length-delimited occurrence of one field."""
    return [(vs, ve) for f, wire, _k, vs, ve, _v in _pb_fields(buf, start, end)
            if f == field and wire == 2]


def _pb_rebuild(buf: bytes, start: int, end: int, handler) -> bytes:
    """Re-emit a message, letting handler drop or replace length-delimited fields.

    handler(field, value_start, value_end) returns None to keep the field
    verbatim, _DROP to remove it, or bytes to use as its new payload.
    """
    out = bytearray()
    for field, wire, key_start, vs, ve, _v in _pb_fields(buf, start, end):
        action = handler(field, vs, ve) if wire == 2 else None
        if action is None:
            out += buf[key_start:ve]
        elif action is not _DROP:
            out += _pb_encode_varint((field << 3) | 2) + _pb_encode_varint(len(action)) + action
    return bytes(out)


def _world_record_spans(body: bytes) -> list[tuple[int, int]]:
    world = _pb_spans(body, 0, len(body), _BODY_WORLD)
    if len(world) != 1:
        raise TransferError("Unsupported save layout: no world data.")
    level = _pb_spans(body, *world[0], 2)
    if len(level) != 1:
        raise TransferError("Unsupported save layout: unexpected world structure.")
    return [rec for box in _pb_spans(body, *level[0], 2) for rec in _pb_spans(body, *box, 2)]


def _foliage_mesh(body: bytes, start: int, end: int, class_index: int,
                  names: list[str]) -> str | None:
    """Mesh type of a foliage record, or None for any other kind of record."""
    cls = identity = None
    for field, wire, _k, vs, ve, value in _pb_fields(body, start, end):
        if field == _RECORD_CLASS and wire == 0:
            cls = value
        elif field == _RECORD_IDENTITY and wire == 2:
            identity = (vs, ve)
    if cls != class_index or identity is None:
        return None
    for field, wire, _k, _vs, _ve, value in _pb_fields(body, *identity):
        if field == 1 and wire == 0:
            return names[value] if value < len(names) else None
    return None


def strip_foliage_records(body: bytes, mesh_pattern: str) -> tuple[bytes, dict[str, int]]:
    """Remove foliage records whose mesh type fully matches mesh_pattern."""
    import collections
    import re

    names = [body[vs:ve].decode("utf-8", "replace")
             for vs, ve in _pb_spans(body, 0, len(body), _BODY_STRINGS)]
    try:
        class_index = names.index(FOLIAGE_DATA_CLASS)
    except ValueError:
        raise TransferError("Unsupported save layout: no foliage records.") from None
    matcher = re.compile(mesh_pattern)
    removed: collections.Counter = collections.Counter()

    def in_container(field, vs, ve):
        if field == 2:
            mesh = _foliage_mesh(body, vs, ve, class_index, names)
            if mesh is not None and matcher.fullmatch(mesh):
                removed[mesh] += 1
                return _DROP
        return None

    def in_level(field, vs, ve):
        return _pb_rebuild(body, vs, ve, in_container) if field == 2 else None

    def in_world(field, vs, ve):
        return _pb_rebuild(body, vs, ve, in_level) if field == 2 else None

    def in_body(field, vs, ve):
        return _pb_rebuild(body, vs, ve, in_world) if field == _BODY_WORLD else None

    return _pb_rebuild(body, 0, len(body), in_body), dict(removed)


def _verify_strip(before: bytes, after: bytes, removed_total: int) -> None:
    """Prove that only the intended records went away."""
    old = [before[s:e] for s, e in _world_record_spans(before)]
    new = [after[s:e] for s, e in _world_record_spans(after)]
    if len(old) - len(new) != removed_total:
        raise TransferError("Record count after editing does not add up; not writing.")
    remaining = iter(old)
    for record in new:
        if not any(record == candidate for candidate in remaining):
            raise TransferError("A record that should have survived changed; not writing.")

    def outside_world(body: bytes):
        return [(f, body[vs:ve]) for f, _w, _k, vs, ve, _v in _pb_fields(body, 0, len(body))
                if f != _BODY_WORLD]

    if outside_world(before) != outside_world(after):
        raise TransferError("Data outside the world changed; not writing.")


def respawn_nodes(raw: bytes, oodle: Oodle,
                  mesh_pattern: str = ORE_MESH_PATTERN) -> tuple[bytes, dict[str, int]]:
    """Return a copy of a save with matching harvest nodes reset, and what was reset.

    Checked end to end before returning: every surviving record is byte-identical
    and in order, and the repacked file decompresses back to exactly the edited
    body.
    """
    body = unpack_body(raw, oodle)
    new_body, removed = strip_foliage_records(body, mesh_pattern)
    if not removed:
        return raw, removed
    _verify_strip(body, new_body, sum(removed.values()))
    new_raw = pack_body(raw, new_body, oodle)
    if unpack_body(new_raw, oodle) != new_body:
        raise TransferError("Recompressed save did not round-trip; not writing.")
    return new_raw, removed


def write_respawned(target: Path, new_raw: bytes, oodle: Oodle,
                    mesh_pattern: str = ORE_MESH_PATTERN, log=print) -> int:
    """Replace a save with its respawned version, then check it on disk.

    Snapshots the folder first. Returns how many matching records the file
    still holds when read back -- 0 means every targeted node was reset.
    """
    import os

    snapshot(target.parent, tag="pre-respawn", log=log)
    # Write beside the target, then swap it in, so a failure can't leave a
    # half-written save. The temp name has no ".sav" so nothing mistakes it
    # for a save if it is ever left behind.
    tmp = target.with_name(f"{target.stem}.respawn-tmp")
    tmp.write_bytes(new_raw)
    os.replace(tmp, target)

    written = target.read_bytes()
    if written != new_raw:
        raise TransferError(
            f"{target.name} on disk does not match what was written; "
            "restore the pre-respawn snapshot."
        )
    # Counting with the same matcher that did the removal; nothing is rebuilt.
    remaining = strip_foliage_records(unpack_body(written, oodle), mesh_pattern)[1]
    return sum(remaining.values())


def ore_by_metal(removed: dict[str, int]) -> dict[str, int]:
    """Collapse per-mesh counts into Copper / Tin / Iron (and Other, if any)."""
    totals = {"Copper": 0, "Tin": 0, "Iron": 0, "Other": 0}
    for mesh, count in removed.items():
        lowered = mesh.lower()
        metal = next((m for m in ("Copper", "Tin", "Iron") if m.lower() in lowered), "Other")
        totals[metal] += count
    return {metal: count for metal, count in totals.items() if count}


# --------------------------------------------------------------------------
# Command line interface
# --------------------------------------------------------------------------


def _format_sets(sets: list[SaveSet]) -> str:
    """Render save sets as an aligned table."""
    if not sets:
        return "  (no save files found)"

    rows = []
    for item in sets:
        when = item.saved.strftime("%Y-%m-%d %H:%M") if item.saved else "unknown"
        rows.append(
            (
                item.name,
                str(len(item.files)),
                f"{item.total_bytes / 1e6:.1f} MB",
                item.label,
                item.map_name,
                when,
            )
        )

    headers = ("SAVE", "FILES", "SIZE", "LABEL", "MAP", "LAST SAVED")
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    line = "  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    out = [line, "  " + "  ".join("-" * w for w in widths)]
    out += ["  " + "  ".join(c.ljust(w) for c, w in zip(row, widths)) for row in rows]

    warnings = [s.name for s in sets if not s.name_matches_header]
    if warnings:
        out.append(f"\n  note: filename and in-file character differ for {', '.join(warnings)}")
    return "\n".join(out)


def _require_game_closed(force: bool) -> None:
    if game_is_running() and not force:
        raise TransferError(
            "Bellwright is running. Close the game first so no save is mid-write "
            "(or pass --force if you are sure)."
        )


def _resolve_default_source(explicit: str | None) -> Path:
    """Use --from if given, else a single obvious USB/drive folder."""
    if explicit:
        return Path(explicit)
    candidates = find_candidate_sources()
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise TransferError("No source given. Use --from <folder or .zip>.")
    listing = ", ".join(str(c) for c in candidates)
    raise TransferError(f"Several possible sources ({listing}). Pick one with --from.")


def _resolve_save_file(name: str, steam_id: str | None) -> Path:
    """A path to a .sav, or a slot name like RedDevil_1 in your own folder."""
    candidate = Path(name)
    if candidate.is_file():
        return candidate
    folder = local_save_folder(steam_id)
    filename = name if name.lower().endswith(".sav") else f"{name}.sav"
    path = folder / filename
    if path.is_file():
        return path
    slots = sorted(p for p in folder.glob("*.sav") if is_save_file(p))
    listing = "\n".join(f"  {p.stem:28} {parse_header(p).display_label}" for p in slots)
    raise TransferError(f"No save named {filename} in {folder}. Saves there:\n{listing}")


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="bellwright_save_converter",
        description="Move Bellwright co-op saves between Steam accounts.",
        epilog="Run with no arguments to open the GUI.",
    )
    parser.add_argument("--steam-id", help="override the detected local SteamID64")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("whoami", help="show the detected Steam ID and save folder")

    p_list = sub.add_parser("list", help="show save sets in a folder or zip")
    p_list.add_argument("--from", dest="source", help="folder or .zip (default: auto-detect)")
    p_list.add_argument("--mine", action="store_true", help="list your own saves instead")

    p_import = sub.add_parser("import", help="copy saves into your own folder")
    p_import.add_argument("--from", dest="source", help="folder or .zip (default: auto-detect)")
    p_import.add_argument("--save", action="append", help="save set name; repeatable (default: all)")
    p_import.add_argument("--dry-run", action="store_true", help="show what would happen")
    p_import.add_argument("--no-backups", action="store_true", help="skip .sav_backup0 files")
    p_import.add_argument("--no-snapshot", action="store_true", help="skip the safety snapshot")
    p_import.add_argument("--force", action="store_true", help="proceed even if the game is open")

    p_export = sub.add_parser("export", help="zip your saves to hand to someone else")
    p_export.add_argument("--save", action="append", help="save set name; repeatable (default: all)")
    p_export.add_argument("--to", required=True, help="output .zip path")
    p_export.add_argument("--from", dest="source", help="folder to export (default: your own)")
    p_export.add_argument("--no-backups", action="store_true", help="skip .sav_backup0 files")

    sub.add_parser("backup", help="snapshot your current saves")

    p_restore = sub.add_parser("restore", help="restore a snapshot")
    p_restore.add_argument("archive", nargs="?", help="snapshot .zip (default: the newest)")
    p_restore.add_argument("--force", action="store_true", help="proceed even if the game is open")

    p_respawn = sub.add_parser(
        "respawn", help="reset harvested ore nodes in one of your saves (experimental)"
    )
    p_respawn.add_argument("save", help="slot in your folder, e.g. RedDevil_1, or a path to a .sav")
    p_respawn.add_argument("--dry-run", action="store_true", help="report what would be reset")
    p_respawn.add_argument("--out", help="write the result here instead of replacing the save")
    p_respawn.add_argument("--oodle", help="path to an oo2core_*_win64.dll (default: search)")
    p_respawn.add_argument(
        "--mesh",
        default=ORE_MESH_PATTERN,
        help="regex of foliage mesh types to reset (default: copper, tin and iron ore; "
        "only the default has been tested in game)",
    )
    p_respawn.add_argument(
        "--force",
        action="store_true",
        help="proceed even if the game is open or the save format is untested",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        # No arguments means someone double-clicked the file; show the GUI.
        return run_gui()

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    try:
        return _dispatch(args)
    except (TransferError, DetectionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(args) -> int:
    steam_id = args.steam_id
    if steam_id and not is_steam_id64(steam_id):
        raise TransferError(f"Not a valid SteamID64: {steam_id}")

    if args.command == "whoami":
        detected = steam_id or detect_local_steam_id()
        folder = local_save_folder(detected)
        print(f"Steam ID:    {detected}")
        print(f"Save folder: {folder}")
        print(f"Exists:      {'yes' if folder.is_dir() else 'no'}")
        print(f"Backups:     {backups_root()}")
        return 0

    if args.command == "list":
        source = local_save_folder(steam_id) if args.mine else _resolve_default_source(args.source)
        with resolve_source(source) as folder:
            print(f"\n{source}\n")
            print(_format_sets(scan_folder(folder)))
            print()
        return 0

    if args.command == "import":
        _require_game_closed(args.force)
        source = _resolve_default_source(args.source)
        dest = local_save_folder(steam_id)
        with resolve_source(source) as folder:
            planned = plan_import(
                folder, dest, names=args.save, include_backups=not args.no_backups
            )
            if not args.dry_run and not args.no_snapshot:
                snapshot(dest, tag="pre-import")
            execute_import(planned, dry_run=args.dry_run)
        return 0

    if args.command == "export":
        source = Path(args.source) if args.source else local_save_folder(steam_id)
        export_zip(
            source,
            Path(args.to),
            names=args.save,
            include_backups=not args.no_backups,
        )
        return 0

    if args.command == "backup":
        snapshot(local_save_folder(steam_id), tag="manual")
        return 0

    if args.command == "restore":
        _require_game_closed(args.force)
        if args.archive:
            archive = Path(args.archive)
        else:
            snapshots = list_snapshots()
            if not snapshots:
                raise TransferError(f"No snapshots found in {backups_root()}")
            archive = snapshots[0]
            print(f"Restoring newest snapshot: {archive.name}")
        dest = local_save_folder(steam_id)
        with resolve_source(archive) as folder:
            execute_import(plan_import(folder, dest))
        return 0

    if args.command == "respawn":
        return _respawn(args, steam_id)

    raise TransferError(f"Unknown command: {args.command}")


def _respawn(args, steam_id: str | None) -> int:
    target = _resolve_save_file(args.save, steam_id)
    if not args.dry_run and not args.out:
        _require_game_closed(args.force)

    header = parse_header(target)
    if not header.recognized:
        raise TransferError(f"{target.name} is not a Bellwright save.")
    if header.version != RESPAWN_TESTED_VERSION and not args.force:
        raise TransferError(
            f"{target.name} is format v{header.version}; respawn is only tested on "
            f"v{RESPAWN_TESTED_VERSION}. Try --out first, then --force."
        )

    oodle = Oodle(find_oodle(args.oodle))
    new_raw, removed = respawn_nodes(target.read_bytes(), oodle, args.mesh)

    when = header.saved.strftime("%Y-%m-%d %H:%M") if header.saved else "unknown"
    print(f"\n{target.name}  ({header.display_label}, saved {when})")
    if not removed:
        print("  No harvested nodes of that kind are recorded; nothing to reset.")
        return 0
    for mesh, count in sorted(removed.items()):
        print(f"  {count:5}  {mesh}")
    print(f"  {sum(removed.values()):5}  nodes reset")

    if args.dry_run:
        print("\nDry run: nothing written.")
        return 0

    if args.out:
        out = Path(args.out)
        if out.exists():
            raise TransferError(f"{out} already exists; not overwriting.")
        out.write_bytes(new_raw)
        print(f"\nWrote {out}")
        return 0

    left = write_respawned(target, new_raw, oodle, args.mesh)
    if left:
        raise TransferError(
            f"Check failed: {left} matching records still in {target.name}; "
            "restore the pre-respawn snapshot."
        )
    print(f"\nReset {target.name} in place. Read it back from disk: 0 matching records left.")
    print(f"Load '{header.display_label}' in game.")
    return 0


# --------------------------------------------------------------------------
# Graphical interface
# --------------------------------------------------------------------------
#
# Deliberately plain: the person on the other end of a swap may not be the one
# who set this up, so every action that writes states what it will do first,
# and the window always shows which Steam account is receiving the files.


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
            messagebox.showinfo("Nothing to reset", f"No harvested ore is recorded in {target.name}.")
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
            messagebox.showerror("Close Bellwright first", "Bellwright was started; nothing was written.")
            return

        def commit(log):
            return write_respawned(target, new_raw, oodle, log=log)

        self._in_background(
            commit,
            lambda left, error: self._respawn_finished(target, header, total, breakdown, left, error),
        )

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

if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
