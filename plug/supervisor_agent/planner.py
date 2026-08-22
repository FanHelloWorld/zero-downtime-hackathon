"""The behind-the-scenes planning pass.

Same shape as plan mode: read what is happening, decide what you *would* say,
write it down, and take no action. The agent runs this on every group burst and
speaks only when addressed, so most plans are never used — which is the point.
Reading the room continuously is what makes the eventual reply land;
reconstructing the mood after the fact from stored history does not.

Structured output rather than prose parsing: the plan feeds the reply prompt and
an HTTP endpoint, so a malformed one should be detectable rather than quietly
producing a nonsense register.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

import anthropic

from plug import events
from plug.config import Config
from plug.models import Batch

from . import personality

STAGE = "planner"

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "read": {
            "type": "string",
            "description": "What is actually happening in this chat right now, in a sentence or two.",
        },
        "tone": {
            "type": "string",
            "description": "The room's mood in a few words, e.g. 'winding down, mild bickering'.",
        },
        "threads": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Live, unresolved topics. Empty if nothing is open.",
        },
        "addressed_to_us": {
            "type": "boolean",
            "description": "Does the latest message seem aimed at you specifically?",
        },
        "intent": {
            "type": "string",
            "description": "What you would say if invited to speak right now. One line.",
        },
        "pillar": {
            "type": "string",
            "enum": list(personality.PILLARS),
            "description": "Which register to lead with if you do speak.",
        },
        "pillar_reason": {
            "type": "string",
            "description": "Why that register suits this moment.",
        },
    },
    "required": [
        "read", "tone", "threads", "addressed_to_us",
        "intent", "pillar", "pillar_reason",
    ],
    "additionalProperties": False,
}

SYSTEM = """
You are a member of a small group chat between friends, and you are currently
just listening. Read the conversation and produce a plan — you are NOT replying
and nothing you write here is sent to anyone.

Your job is to stay oriented: who is talking, what the mood is, what is
unresolved, and what you would say if someone pulled you in.

Choose the register you would lead with from these four:
{menu}

Pick the one that fits the room, not the one you like. A group winding down
after bad news does not want enthusiasm; a group hyping someone up does not want
deadpan. Reading the room correctly matters more than being funny.

`addressed_to_us` is your own read only. It does not decide whether you speak —
that is settled elsewhere — so answer it honestly rather than strategically.
""".strip()


@dataclass(slots=True)
class Plan:
    read: str = ""
    tone: str = ""
    threads: list[str] = field(default_factory=list)
    addressed_to_us: bool = False
    intent: str = ""
    pillar: str = personality.DEFAULT_PILLAR.key
    pillar_reason: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | None) -> "Plan | None":
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return cls(**{k: v for k, v in data.items() if k in cls.__slots__})

    def brief(self) -> str:
        """The plan as the reply prompt sees it."""
        lines = [f"Your read of the room: {self.read}", f"Mood: {self.tone}"]
        if self.threads:
            lines.append("Open threads: " + "; ".join(self.threads))
        if self.intent:
            lines.append(f"What you were planning to say: {self.intent}")
        return "\n".join(lines)


class Planner:
    def __init__(self, config: Config, client: anthropic.Anthropic | None = None) -> None:
        self.config = config
        self.client = client or anthropic.Anthropic()

    def _prompt(self, batch: Batch, previous: Plan | None) -> str:
        roster = ", ".join(batch.roster) or "unknown"
        parts = [
            f"Group: {batch.last.chat_label}",
            f"Members: {roster}",
        ]
        if previous:
            parts.append(
                "\nYour previous read (things may have moved on):\n" + previous.brief()
            )
        parts.append("\nNew messages:\n" + batch.transcript())
        return "\n".join(parts)

    def plan(self, batch: Batch, previous: Plan | None = None) -> Plan | None:
        """Produce a plan for this burst, or None if planning failed.

        Failure degrades to "no plan" rather than raising: a missing read costs
        the reply some context, while an exception here would dead-letter a
        message the agent was never going to answer anyway.
        """
        try:
            response = self.client.messages.create(
                model=self.config.model,
                max_tokens=self.config.group.plan_max_tokens,
                system=SYSTEM.format(menu=personality.menu()),
                messages=[{"role": "user", "content": self._prompt(batch, previous)}],
                output_config={"format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
            )
        except anthropic.APIError as exc:
            events.emit(STAGE, "failed", chat=batch.chat_guid, error=str(exc))
            return None

        text = next((b.text for b in response.content if b.type == "text"), None)
        plan = Plan.from_json(text)
        if plan is None:
            events.emit(STAGE, "unparseable", chat=batch.chat_guid)
            return None

        events.emit(
            STAGE, "planned", chat=batch.chat_guid,
            tone=plan.tone, pillar=plan.pillar,
            threads=len(plan.threads), model_thinks_addressed=plan.addressed_to_us,
        )
        return plan
