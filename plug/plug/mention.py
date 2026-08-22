"""Deciding whether the agent was actually addressed.

In a group chat the agent reads everything and speaks almost never, so this is
the gate that separates the two. It is deliberately conservative: staying quiet
when invited is a missed beat, but interjecting uninvited into three friends'
conversation is the failure that makes people turn the thing off.

Why text matching rather than iMessage's own mention data: ``has_unseen_mention``
is an *unseen* flag, cleared the moment the message is read. Nine rows out of
156,000 have it set on this machine, three of them inbound group messages. It
tells you about notification state, not about who was addressed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Batch, Message


@dataclass(frozen=True, slots=True)
class Tag:
    """Whether the agent was addressed, and what makes us think so."""

    matched: bool
    reason: str = ""
    by: str | None = None
    """Handle of whoever addressed us."""
    text: str = ""
    """The message that did it, for the event log."""

    def __bool__(self) -> bool:
        return self.matched


NO_TAG = Tag(False, "not addressed")

# "hey plug", "plug?", "@plug" — but not "unplugged", "plugin", "plug-in".
# \b alone would still match inside "plug-in", so the trailing guard excludes a
# hyphen followed by a letter.
_BOUNDARY_SUFFIX = r"(?![\w-]*[A-Za-z])"

# A question aimed at someone, used only when answer_direct_questions is on.
_SECOND_PERSON = re.compile(r"\b(you|your|u|ur|y'?all)\b", re.IGNORECASE)


def _name_pattern(names: list[str]) -> re.Pattern[str] | None:
    cleaned = [re.escape(n.strip()) for n in names if n and n.strip()]
    if not cleaned:
        return None
    alternation = "|".join(cleaned)
    return re.compile(rf"(?<![\w-])@?({alternation}){_BOUNDARY_SUFFIX}", re.IGNORECASE)


class MentionDetector:
    """Matches the agent's name and aliases against incoming text."""

    def __init__(
        self,
        name: str,
        aliases: list[str] | None = None,
        *,
        answer_direct_questions: bool = False,
    ) -> None:
        self.name = name
        self.aliases = list(aliases or [])
        self.answer_direct_questions = answer_direct_questions
        self._pattern = _name_pattern([name, *self.aliases])

    def in_message(self, message: Message) -> Tag:
        if self._pattern is None:
            return NO_TAG
        match = self._pattern.search(message.body)
        if not match:
            return NO_TAG
        return Tag(
            True,
            reason=f"named ({match.group(1)!r})",
            by=message.handle,
            text=message.body,
        )

    def find(self, batch: Batch, *, we_spoke_recently: bool = False) -> Tag:
        """Scan a burst for an address to us.

        Later messages win: if someone names us and then keeps talking, the tag
        still stands, but the most recent invitation is the one we answer.
        """
        for message in reversed(batch.messages):
            tag = self.in_message(message)
            if tag:
                return tag

        if self.answer_direct_questions and we_spoke_recently:
            last = batch.last
            if last.body.rstrip().endswith("?") and _SECOND_PERSON.search(last.body):
                return Tag(
                    True,
                    reason="direct question after we spoke",
                    by=last.handle,
                    text=last.body,
                )

        return NO_TAG
