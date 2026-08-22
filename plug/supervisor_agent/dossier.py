"""What the agent knows about a chat, accumulated over time.

The planner reads every burst but speaks almost never, and most of what it hears
is only useful later: someone gives an address on Tuesday and asks where to eat
on Friday. A single stored plan cannot carry that — each one is a fresh read of a
fresh burst, and anything the model forgets to repeat is gone.

So the plan reports only what a burst *taught* us, and this module merges those
facts into a per-chat dossier: who is where, when they are free, what they will
not eat, and the room's standing vibe. Merging happens in Python rather than by
asking the model to restate the whole picture each time — the same reason
``mention.py`` decides who was addressed. A merge cannot hallucinate a friend or
quietly drop one.

Everything here is pure. Storage is ``Memory.save_dossier``; the model never
writes this structure directly.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field

# Caps, so a long-running chat cannot grow an unbounded prompt.
MAX_PEOPLE = 20
MAX_NOTES = 6
MAX_FIELD_CHARS = 120
MAX_VIBE_CHARS = 200

_UNIT = re.compile(r"\b(?:apt|apartment|unit|suite|ste|floor|fl|#)\s*[\w-]+,?\s*", re.IGNORECASE)
_ZIP = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_HOUSE_NUMBER = re.compile(r"^\d+[a-zA-Z]?\b[\s,-]*")
_STREET_SUFFIX = re.compile(
    r"\b(st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|way|ct|court"
    r"|pl|place|ter|terrace|hwy|highway|pkwy|parkway|cir|circle)\b\.?",
    re.IGNORECASE,
)


def _clip(value: object, limit: int = MAX_FIELD_CHARS) -> str:
    text = str(value or "").strip()
    return text[:limit]


def coarsen_location(raw: str | None) -> str:
    """Reduce a street address to the neighbourhood or city around it.

    Applied before a location reaches a model or an external API. Someone typing
    their address into a group chat is telling their friends, not consenting to
    it being handed to a scraping service — and "which part of town" is all a
    restaurant search actually needs.

    Deliberately conservative: anything it cannot confidently parse is passed
    through unchanged rather than mangled into something wrong.
    """
    text = _clip(raw)
    if not text:
        return ""

    text = _UNIT.sub("", text)
    text = _ZIP.sub("", text)

    parts = [p.strip(" ,") for p in text.split(",")]
    parts = [p for p in parts if p]

    if len(parts) > 1:
        kept = [p for p in parts if not _HOUSE_NUMBER.match(p)]
        if kept:
            return ", ".join(kept).strip(" ,")
        return parts[-1].strip(" ,")

    single = parts[0] if parts else ""
    if not _HOUSE_NUMBER.match(single):
        return single.strip(" ,")

    # One run-on segment starting with a house number: drop the number, then the
    # street name up to and including its suffix, and keep whatever follows.
    without_number = _HOUSE_NUMBER.sub("", single)
    suffix = _STREET_SUFFIX.search(without_number)
    if suffix:
        tail = without_number[suffix.end():].strip(" ,")
        if tail:
            return tail
    return without_number.strip(" ,")


@dataclass(slots=True)
class Person:
    """One member of the chat, as far as we can tell."""

    handle: str
    name: str = ""
    location: str = ""
    availability: str = ""
    notes: list[str] = field(default_factory=list)
    """Standing facts — dietary constraints, a car, a hard curfew."""


@dataclass(slots=True)
class Dossier:
    """Everything carried forward about one chat."""

    people: dict[str, Person] = field(default_factory=dict)
    vibe: str = ""
    occasion: str = ""
    updated_at: float = 0.0

    def to_json(self) -> str:
        payload = {
            "people": {h: asdict(p) for h, p in self.people.items()},
            "vibe": self.vibe,
            "occasion": self.occasion,
            "updated_at": self.updated_at,
        }
        return json.dumps(payload)

    @classmethod
    def from_json(cls, raw: str | None) -> "Dossier":
        """Parse a stored dossier, degrading to an empty one rather than raising.

        A corrupt dossier should cost context, never a reply.
        """
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return cls()
        if not isinstance(data, dict):
            return cls()

        people: dict[str, Person] = {}
        for handle, entry in (data.get("people") or {}).items():
            if not isinstance(entry, dict):
                continue
            notes = entry.get("notes") or []
            people[handle] = Person(
                handle=handle,
                name=_clip(entry.get("name")),
                location=_clip(entry.get("location")),
                availability=_clip(entry.get("availability")),
                notes=[_clip(n) for n in notes if str(n or "").strip()][:MAX_NOTES],
            )
        return cls(
            people=people,
            vibe=_clip(data.get("vibe"), MAX_VIBE_CHARS),
            occasion=_clip(data.get("occasion")),
            updated_at=float(data.get("updated_at") or 0.0),
        )

    def known_locations(self) -> list[str]:
        return [p.location for p in self.people.values() if p.location]

    def brief(self, *, coarse: bool = True) -> str:
        """Render for a prompt. Empty string when we know nothing worth saying."""
        lines: list[str] = []
        if self.occasion:
            lines.append(f"On the table: {self.occasion}")
        if self.vibe:
            lines.append(f"Standing vibe: {self.vibe}")

        for person in self.people.values():
            bits = []
            if person.location:
                bits.append(coarsen_location(person.location) if coarse else person.location)
            if person.availability:
                bits.append(f"free {person.availability}")
            if person.notes:
                bits.append("; ".join(person.notes))
            if bits:
                who = person.name or person.handle
                lines.append(f"- {who}: {', '.join(bits)}")

        if not lines:
            return ""
        return "\n".join(["What you know about this group:", *lines])


def merge(
    dossier: Dossier | None,
    facts: list[dict] | None,
    *,
    vibe: str = "",
    occasion: str = "",
    now: float | None = None,
) -> Dossier:
    """Fold one burst's newly-learned facts into the dossier.

    New values overwrite old ones for the same field — people move, plans change,
    and the most recent statement is the one to believe. A field the burst did
    not mention is left alone, which is what makes silence safe: the model
    reporting nothing about Ana this burst must not erase what Ana said before.
    """
    merged = Dossier(
        people=dict((dossier.people if dossier else {})),
        vibe=(dossier.vibe if dossier else ""),
        occasion=(dossier.occasion if dossier else ""),
    )

    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        handle = _clip(fact.get("who"))
        if not handle:
            continue

        person = merged.people.get(handle) or Person(handle=handle)
        # Copy before mutating: the caller's dossier is not ours to edit.
        person = Person(
            handle=person.handle,
            name=person.name,
            location=person.location,
            availability=person.availability,
            notes=list(person.notes),
        )

        if _clip(fact.get("name")):
            person.name = _clip(fact.get("name"))
        if _clip(fact.get("location")):
            person.location = _clip(fact.get("location"))
        if _clip(fact.get("availability")):
            person.availability = _clip(fact.get("availability"))

        note = _clip(fact.get("note"))
        if note and note not in person.notes:
            person.notes.append(note)
            person.notes = person.notes[-MAX_NOTES:]

        merged.people[handle] = person

    if len(merged.people) > MAX_PEOPLE:
        # Keep the most recently touched. dicts preserve insertion order and a
        # re-assignment above does not move a key, so drop from the front.
        surplus = len(merged.people) - MAX_PEOPLE
        for handle in list(merged.people)[:surplus]:
            del merged.people[handle]

    if _clip(vibe, MAX_VIBE_CHARS):
        merged.vibe = _clip(vibe, MAX_VIBE_CHARS)
    if _clip(occasion):
        merged.occasion = _clip(occasion)

    merged.updated_at = time.time() if now is None else now
    return merged
