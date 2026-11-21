"""Schema-less protobuf wire format: reading fields and rewriting messages."""

from __future__ import annotations

from .transfer import TransferError


DROP = object()


def read_varint(buf: bytes, o: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[o]
        o += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, o
        shift += 7


def encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def fields(buf: bytes, start: int, end: int):
    """Yield (field, wire_type, key_start, value_start, value_end, int_value)."""
    o = start
    while o < end:
        key_start = o
        key, o = read_varint(buf, o)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, nxt = read_varint(buf, o)
            yield field, wire, key_start, o, nxt, value
            o = nxt
        elif wire == 1:
            yield field, wire, key_start, o, o + 8, None
            o += 8
        elif wire == 5:
            yield field, wire, key_start, o, o + 4, None
            o += 4
        elif wire == 2:
            length, o = read_varint(buf, o)
            if o + length > end:
                raise TransferError("Corrupt save body: a field overruns its message.")
            yield field, wire, key_start, o, o + length, length
            o += length
        else:
            raise TransferError(f"Corrupt save body: unknown wire type {wire}.")
    if o != end:
        raise TransferError("Corrupt save body: misaligned message.")


def spans(buf: bytes, start: int, end: int, field: int) -> list[tuple[int, int]]:
    """Value ranges of every length-delimited occurrence of one field."""
    return [(vs, ve) for f, wire, _k, vs, ve, _v in fields(buf, start, end)
            if f == field and wire == 2]


def rebuild(buf: bytes, start: int, end: int, handler) -> bytes:
    """Re-emit a message, letting handler drop or replace length-delimited fields.

    handler(field, value_start, value_end) returns None to keep the field
    verbatim, DROP to remove it, or bytes to use as its new payload.
    """
    out = bytearray()
    for field, wire, key_start, vs, ve, _v in fields(buf, start, end):
        action = handler(field, vs, ve) if wire == 2 else None
        if action is None:
            out += buf[key_start:ve]
        elif action is not DROP:
            out += encode_varint((field << 3) | 2) + encode_varint(len(action)) + action
    return bytes(out)
