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
