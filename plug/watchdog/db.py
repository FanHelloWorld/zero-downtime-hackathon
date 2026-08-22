"""Read-only access to the Messages database.

Two rules this module exists to enforce:

1. Open with ``?mode=ro``, never ``?immutable=1``. chat.db runs in WAL mode, and
   an immutable connection ignores the -wal file: measured on this machine it
   reported max ROWID 176617 while the true value was 176627. The ten messages
   it misses are exactly the newest ones — the whole point of the watchdog.
2. Never open a writable connection. Plug's own state lives in a separate
   database (``plug.memory``); chat.db is treated as strictly read-only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from plug.config import CHAT_DB
from .decode import body_of
from plug.models import Message, apple_time_to_datetime

# associated_message_type != 0 marks tapbacks (2000-2006 observed) and replies to
# them; item_type != 0 marks joins/leaves/renames and other system rows. Neither
# should wake the agent.
_NEW_MESSAGES_SQL = """
SELECT m.ROWID          AS rowid,
       m.guid           AS guid,
       m.text           AS text,
       m.attributedBody AS attributed_body,
       m.date           AS date,
       h.id             AS handle,
       c.guid           AS chat_guid,
       c.chat_identifier AS chat_identifier,
       c.service_name   AS service_name,
       c.style          AS style,
       c.display_name   AS display_name
FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat c               ON c.ROWID = cmj.chat_id
LEFT JOIN handle h        ON h.ROWID = m.handle_id
WHERE m.ROWID > :cursor
  AND m.is_from_me = 0
  AND m.associated_message_type = 0
  AND m.item_type = 0
ORDER BY m.ROWID ASC
LIMIT :limit
"""


class ChatDB:
    """A read-only handle on chat.db."""

    def __init__(self, path: Path | str = CHAT_DB) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"Messages database not found at {self.path}")
        self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ChatDB":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def max_rowid(self) -> int:
        row = self._conn.execute("SELECT MAX(ROWID) FROM message").fetchone()
        return int(row[0] or 0)

    def journal_mode(self) -> str:
        return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0])

    def messages_after(self, cursor: int, limit: int = 200) -> list[Message]:
        """Inbound, human-authored messages with ROWID greater than ``cursor``.

        A message joined to several chats yields several rows; we keep the first
        so the agent replies once.
        """
        rows = self._conn.execute(
            _NEW_MESSAGES_SQL, {"cursor": cursor, "limit": limit}
        ).fetchall()

        seen: set[int] = set()
        out: list[Message] = []
        for row in rows:
            rowid = int(row["rowid"])
            if rowid in seen:
                continue
            seen.add(rowid)

            body = body_of(row["text"], row["attributed_body"])
            if not body or not body.strip():
                # Attachment-only messages and undecodable rows have nothing to
                # reply to. The cursor still advances past them.
                continue

            out.append(
                Message(
                    rowid=rowid,
                    guid=row["guid"],
                    body=body.strip(),
                    sent_at=apple_time_to_datetime(row["date"]),
                    handle=row["handle"],
                    chat_guid=row["chat_guid"],
                    chat_identifier=row["chat_identifier"] or "",
                    service=row["service_name"] or "unknown",
                    style=int(row["style"] or 0),
                    display_name=row["display_name"] or None,
                )
            )
        return out

    def decode_samples(self, limit: int = 300) -> list[tuple[str, bytes]]:
        """Rows carrying both ``text`` and ``attributedBody``, for decoder tests.

        Decoding the blob must reproduce ``text`` exactly, which gives the
        decoder a real correctness signal without checking fixtures into the
        repo or exposing message content outside the machine.
        """
        rows = self._conn.execute(
            """
            SELECT text, attributedBody
            FROM message
            WHERE text IS NOT NULL
              AND attributedBody IS NOT NULL
              AND length(text) > 0
            ORDER BY ROWID DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [(r[0], bytes(r[1])) for r in rows]
