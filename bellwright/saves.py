"""Bellwright save files: reading the header and grouping files by character."""

from __future__ import annotations

import dataclasses
import datetime as _dt
import struct
from pathlib import Path


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
#   ...           UE5 compressed archive, located by ARCHIVE_TAG in
#                 respawn.py

MAGIC = b"VSWB"
HEADER_SIZE = 0x420

_OFF_VERSION = 0x04
_OFF_CREATED = 0x08
_OFF_SAVED = 0x10
OFF_BODY_SIZE = 0x18
_OFF_CHARACTER = 0x20
_OFF_MAP = 0x120
_OFF_LABEL = 0x220

_FIELD_SIZE = 0x100


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
        body_size=_read_u64(buf, OFF_BODY_SIZE),
        character=_read_string(buf, _OFF_CHARACTER),
        map_name=_read_string(buf, _OFF_MAP),
        label=_read_string(buf, _OFF_LABEL),
    )


#: Steam's own bookkeeping file. It carries the *account id of whoever owns the
#: folder*, so it must never be copied between folders -- doing so would point
#: the destination folder at the wrong account.
AUTOCLOUD_FILE = "steam_autocloud.vdf"


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
