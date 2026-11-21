"""Finding and calling Oodle, the compression library Bellwright saves use."""

from __future__ import annotations

import struct
from pathlib import Path

from .steam import steam_library_paths
from .transfer import TransferError


_OODLE_KRAKEN = 8
_OODLE_LEVEL_NORMAL = 4


class OodleError(TransferError):
    """The Oodle library could not be found or used."""


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
    the launcher script, then every Steam library.
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

    here = Path(__file__).resolve().parent.parent  # beside the launcher script
    beside = [p for p in here.iterdir() if p.is_file() and _is_oodle_dll(p.name)]
    if beside:
        return max(beside, key=_oodle_version)

    log("Searching Steam libraries for an Oodle DLL...")
    found: list[Path] = []
    for library in steam_library_paths():
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
            "next to bellwright_save_converter.py, or pass --oodle <path>."
        )
    best = max(found, key=_oodle_version)
    log(f"Using {best}  (copy it next to bellwright_save_converter.py to skip the search)")
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
