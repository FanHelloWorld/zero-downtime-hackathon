"""Decode Apple's typedstream ``attributedBody`` blobs into plain text.

Recent macOS releases increasingly leave ``message.text`` NULL and store the
body only in ``message.attributedBody`` — a serialized ``NSAttributedString``
in Apple's binary "typedstream" (NSArchiver) format. On this machine roughly
600 of the last 1600 messages are in that state, so this is a required path,
not a fallback.

We don't need a general typedstream parser. The message body is the first
NSString payload in the archive, so we locate the ``NSString`` class name and
read the length-prefixed UTF-8 that follows it.
"""

from __future__ import annotations

NSSTRING = b"NSString"

# Typedstream tags an inline length-prefixed string payload with the ObjC type
# code '+' (0x2B). It sits a handful of bytes past the class name, after the
# class/version bookkeeping.
STRING_TYPE = 0x2B

# How far past "NSString" to look for that marker before giving up.
_MARKER_WINDOW = 24

# Sanity bound: iMessage bodies are not megabytes of text. A "length" larger
# than this means we locked onto the wrong byte.
_MAX_LEN = 1 << 20


def _read_int(data: bytes, i: int) -> tuple[int, int]:
    """Read a typedstream-encoded integer at ``i``.

    Small values are a single byte. 0x81/0x82/0x83 introduce a 2/4/8-byte
    little-endian value. Returns ``(value, index_after)``.
    """
    tag = data[i]
    if tag == 0x81:
        return int.from_bytes(data[i + 1 : i + 3], "little"), i + 3
    if tag == 0x82:
        return int.from_bytes(data[i + 1 : i + 5], "little"), i + 5
    if tag == 0x83:
        return int.from_bytes(data[i + 1 : i + 9], "little"), i + 9
    return tag, i + 1


def _try_at(data: bytes, marker: int) -> str | None:
    """Attempt to read a string whose 0x2B marker sits at ``marker``."""
    length, start = _read_int(data, marker + 1)
    if length <= 0 or length > _MAX_LEN or start + length > len(data):
        return None
    try:
        return data[start : start + length].decode("utf-8")
    except UnicodeDecodeError:
        return None


def decode_attributed_body(blob: bytes | memoryview | None) -> str | None:
    """Extract the message body from an ``attributedBody`` blob.

    Returns ``None`` when the blob is empty or holds no decodable string —
    callers treat that as "no readable text" and skip the message.
    """
    if not blob:
        return None
    data = bytes(blob)

    search_from = 0
    while True:
        cls = data.find(NSSTRING, search_from)
        if cls == -1:
            return None
        # Candidate 0x2B markers live just past the class name. Try each in
        # order and take the first that yields well-formed UTF-8 — a stray '+'
        # inside bookkeeping bytes fails the length or decode check.
        scan_start = cls + len(NSSTRING)
        scan_end = min(scan_start + _MARKER_WINDOW, len(data))
        marker = scan_start
        while True:
            marker = data.find(bytes([STRING_TYPE]), marker, scan_end)
            if marker == -1:
                break
            text = _try_at(data, marker)
            if text is not None:
                return text
            marker += 1
        search_from = cls + 1


def body_of(text: str | None, attributed_body: bytes | None) -> str | None:
    """Resolve a message body, preferring the plain ``text`` column."""
    if text:
        return text
    return decode_attributed_body(attributed_body)
