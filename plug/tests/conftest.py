from __future__ import annotations

from datetime import datetime, timezone

import pytest

from supervisor_agent.memory import Memory
from plug.config import Config
from plug.models import STYLE_DIRECT, STYLE_GROUP, Message
from plug.spool import Spool, SpoolItem


@pytest.fixture()
def memory(tmp_path):
    mem = Memory(tmp_path / "supervisor.db")
    yield mem
    mem.close()


@pytest.fixture()
def spool(tmp_path):
    sp = Spool(tmp_path / "spool.db")
    yield sp
    sp.close()


@pytest.fixture()
def config():
    return Config()


def make_message(
    *,
    rowid: int = 1,
    body: str = "hey",
    handle: str = "+15551234567",
    chat_guid: str = "any;-;+15551234567",
    service: str = "iMessage",
    style: int = STYLE_DIRECT,
    participants: tuple[str, ...] = (),
) -> Message:
    return Message(
        rowid=rowid,
        guid=f"guid-{rowid}",
        body=body,
        sent_at=datetime.now(tz=timezone.utc),
        handle=handle,
        chat_guid=chat_guid,
        chat_identifier=handle,
        service=service,
        style=style,
        display_name=None,
        participants=participants,
    )


def make_item(item_id: int = 1, attempts: int = 1, **kwargs) -> SpoolItem:
    """A leased pool item wrapping a synthetic message."""
    return SpoolItem(id=item_id, attempts=attempts, message=make_message(**kwargs))


@pytest.fixture()
def group_message():
    return make_message(chat_guid="any;+;abc123", style=STYLE_GROUP, body="anyone around?")


def imported_roots(module) -> set[str]:
    """Top-level package names a module imports, read from its AST.

    Used to assert the two servers stay independent. Checking the source text
    for a substring would match prose in docstrings; the import graph is the
    thing that actually couples two modules.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots
