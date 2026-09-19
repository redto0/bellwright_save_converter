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
    return ".sav" in name and name != AUTOCLOUD_FILE.lower()


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
