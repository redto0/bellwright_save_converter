"""Resetting harvested ore nodes inside a save.

The one feature that edits a save. After the header the file holds a UE5
compressed archive: an ARCHIVE_TAG header, a chunk table, then 128 KiB chunks
compressed with Oodle Kraken. Decompressed, the world is protobuf.

Every foliage instance the player has touched -- an ore chunk picked up, a
deposit mined -- has one MistFoliageDataComponent record, keyed by (mesh type,
instance id). Untouched foliage has no record at all, so deleting a node's
record puts it back to its pristine, harvestable state. Verified in game for
the ore types below on format version 8.
"""

from __future__ import annotations

import dataclasses
import struct
from pathlib import Path

from . import protobuf as pb
from .oodle import Oodle
from .saves import OFF_BODY_SIZE
from .transfer import TransferError, snapshot


ARCHIVE_TAG = 0x222222229E2A83C1
FOLIAGE_DATA_CLASS = "/Script/Mist.MistFoliageDataComponent"

#: Copper and tin surface pickups, plus copper, iron and tin rock deposits.
#: Matched exactly: a loose "ore" pattern would also hit every "Forest" mesh.
ORE_MESH_PATTERN = r"SM_(CopperOre_\d+|Tin|MEDIUM_(Copper|Iron|Tin)Rock_\d+)"

RESPAWN_TESTED_VERSION = 8

# Body layout as protobuf field numbers: 8 is the global string table and 21
# the world. Inside the world, 2 > 2 are record containers holding records in 2.
_BODY_STRINGS = 8
_BODY_WORLD = 21
_RECORD_CLASS = 5
_RECORD_IDENTITY = 4


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
    struct.pack_into("<Q", header, OFF_BODY_SIZE, len(body))
    return (
        bytes(header)
        + struct.pack("<Qq", ARCHIVE_TAG, size)
        + bytes([archive.compressor])
        + struct.pack("<qq", sum(len(p) for p in pieces), len(body))
        + table
        + b"".join(pieces)
    )


def _world_record_spans(body: bytes) -> list[tuple[int, int]]:
    world = pb.spans(body, 0, len(body), _BODY_WORLD)
    if len(world) != 1:
        raise TransferError("Unsupported save layout: no world data.")
    level = pb.spans(body, *world[0], 2)
    if len(level) != 1:
        raise TransferError("Unsupported save layout: unexpected world structure.")
    return [rec for box in pb.spans(body, *level[0], 2) for rec in pb.spans(body, *box, 2)]


def _foliage_mesh(body: bytes, start: int, end: int, class_index: int,
                  names: list[str]) -> str | None:
    """Mesh type of a foliage record, or None for any other kind of record."""
    cls = identity = None
    for field, wire, _k, vs, ve, value in pb.fields(body, start, end):
        if field == _RECORD_CLASS and wire == 0:
            cls = value
        elif field == _RECORD_IDENTITY and wire == 2:
            identity = (vs, ve)
    if cls != class_index or identity is None:
        return None
    for field, wire, _k, _vs, _ve, value in pb.fields(body, *identity):
        if field == 1 and wire == 0:
            return names[value] if value < len(names) else None
    return None


def strip_foliage_records(body: bytes, mesh_pattern: str) -> tuple[bytes, dict[str, int]]:
    """Remove foliage records whose mesh type fully matches mesh_pattern."""
    import collections
    import re

    names = [body[vs:ve].decode("utf-8", "replace")
             for vs, ve in pb.spans(body, 0, len(body), _BODY_STRINGS)]
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
                return pb.DROP
        return None

    def in_level(field, vs, ve):
        return pb.rebuild(body, vs, ve, in_container) if field == 2 else None

    def in_world(field, vs, ve):
        return pb.rebuild(body, vs, ve, in_level) if field == 2 else None

    def in_body(field, vs, ve):
        return pb.rebuild(body, vs, ve, in_world) if field == _BODY_WORLD else None

    return pb.rebuild(body, 0, len(body), in_body), dict(removed)


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
        return [(f, body[vs:ve]) for f, _w, _k, vs, ve, _v in pb.fields(body, 0, len(body))
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
