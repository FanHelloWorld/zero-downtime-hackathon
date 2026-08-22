"""Decoder correctness, checked against the live Messages database.

Rows that carry both ``text`` and ``attributedBody`` are self-validating: the
decoded blob must equal the plain column. That gives a real signal without
committing fixtures or moving message content off the machine.
"""

from __future__ import annotations

import pytest

from watchdog.decode import body_of, decode_attributed_body

try:
    from watchdog.db import ChatDB

    _db_available = ChatDB().max_rowid() > 0
except Exception:  # pragma: no cover - depends on Full Disk Access
    _db_available = False

needs_db = pytest.mark.skipif(not _db_available, reason="chat.db not readable here")


def test_empty_inputs():
    assert decode_attributed_body(None) is None
    assert decode_attributed_body(b"") is None
    assert decode_attributed_body(b"no marker here") is None


def test_body_prefers_plain_text():
    assert body_of("plain", b"ignored") == "plain"
    assert body_of(None, None) is None
    assert body_of("", None) is None


@needs_db
def test_decodes_match_text_column():
    with ChatDB() as db:
        samples = db.decode_samples(400)
    assert samples, "expected rows with both text and attributedBody"

    mismatches = [
        (text, decode_attributed_body(blob))
        for text, blob in samples
        if decode_attributed_body(blob) != text
    ]
    assert not mismatches, f"{len(mismatches)}/{len(samples)} blobs decoded incorrectly"


@needs_db
def test_decodes_rows_that_have_no_text_column():
    """The rows that actually need the decoder must produce usable strings."""
    import sqlite3

    from plug.config import CHAT_DB

    conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT attributedBody FROM message "
        "WHERE text IS NULL AND attributedBody IS NOT NULL "
        "ORDER BY ROWID DESC LIMIT 200"
    ).fetchall()
    conn.close()

    if not rows:
        pytest.skip("no NULL-text rows present")

    decoded = [decode_attributed_body(bytes(r[0])) for r in rows]
    failures = sum(1 for d in decoded if not d)
    assert failures == 0, f"{failures}/{len(rows)} NULL-text rows failed to decode"
