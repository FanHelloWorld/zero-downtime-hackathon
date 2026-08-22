"""The behind-the-scenes planning pass.

Same shape as plan mode: read what is happening, decide what you *would* say,
write it down, and take no action. The agent runs this on every group burst and
speaks only when addressed, so most plans are never used — which is the point.
Reading the room continuously is what makes the eventual reply land;
reconstructing the mood after the fact from stored history does not.

Structured output rather than prose parsing: the plan feeds the reply prompt and
an HTTP endpoint, so a malformed one should be detectable rather than quietly
producing a nonsense register.

A plan also carries two things the agent can act on. ``facts_learned`` is what
this burst taught us about the people in the room — merged into the chat's
dossier by ``dossier.merge``, in Python, so a burst the model summarises badly
cannot erase what someone said last Tuesday. ``action`` is a proposal: this
question would be answered better by going and looking something up. Both are
advisory in exactly the way ``addressed_to_us`` is advisory — the code decides
whether a worker runs, using quotas the model never sees.
"""


from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field


import anthropic

from plug import events
from plug.config import Config
from plug.models import Batch

from . import personality
from .workers import registry

STAGE = "planner"

_FACT = {
    "type": "object",
    "properties": {
        "who": {"type": "string", "description": "The speaker's handle, exactly as it appears in the transcript."},
        "name": {"type": "string", "description": "What people call them, if you learned it. Empty otherwise."},
        "location": {"type": "string", "description": "Where they are or are coming from. Empty if not mentioned."},
        "availability": {"type": "string", "description": "When they said they are free. Empty if not mentioned."},
        "note": {"type": "string", "description": "One standing fact — a dietary constraint, no car, a curfew. Empty if none."},
    },
    "required": ["who", "name", "location", "availability", "note"],
    "additionalProperties": False,
}


_PLAN_SCHEMA = {
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
        "facts_learned": {
            "type": "array",
            "items": _FACT,
            "description": (
                "Only what THIS burst taught you about people — a location, a time "
                "they are free, a constraint. Do not restate what you already knew; "
                "it is remembered for you. Empty when nothing new was said."
            ),
        },
        "action": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["none"],   # replaced per-config by build_schema
                    "description": "The kind of lookup that would help, or 'none'.",

                },
                "warranted": {
                    "type": "boolean",
                    "description": "Would this conversation actually be better for that lookup?",
                },
                "objective": {
                    "type": "string",
                    "description": "What to find out, in one or two sentences. Empty when kind is 'none'.",
                },
                "missing": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "What you still don't know that would make the lookup better.",
                },
            },
            "required": ["kind", "warranted", "objective", "missing"],
            "additionalProperties": False,
        },
    },
    "required": [
        "read", "tone", "threads", "addressed_to_us",
        "intent", "pillar", "pillar_reason", "facts_learned", "action",
    ],
    "additionalProperties": False,
}


def build_schema(kinds: tuple[str, ...] | list[str]) -> dict:
    """The plan schema, with ``action.kind`` limited to the workers on offer.

    Built per call rather than fixed: offering a kind that is switched off
    invites the model to propose work that will be silently refused, and a plan
    nobody can act on is worse than no proposal.
    """
    schema = copy.deepcopy(_PLAN_SCHEMA)
    schema["properties"]["action"]["properties"]["kind"]["enum"] = ["none", *kinds]
    return schema


PLAN_SCHEMA = build_schema(registry.kinds())



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

Record in `facts_learned` only what this burst told you about people — where
someone is, when they are free, something they cannot eat. What you learned
before is already remembered; repeating it wastes the field, and leaving it out
loses nothing.

In `action`, say whether answering this group well would need you to go and look
something up. Available lookups:
{workers}

Propose one only when the conversation is actually heading somewhere it would
help — people converging on a plan, not people chatting. Say 'none' otherwise.
List what you still don't know in `missing`.

`addressed_to_us` and `action` are your own read only. Neither decides whether
you speak or whether anything runs — that is settled elsewhere — so answer them
honestly rather than strategically.
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
    facts_learned: list[dict] = field(default_factory=list)
    action: dict = field(default_factory=dict)

    @property
    def action_kind(self) -> str:
        """The proposed lookup, or "none". Never raises on a malformed action."""
        if not isinstance(self.action, dict):
            return "none"
        return str(self.action.get("kind") or "none").strip().lower()

    @property
    def action_warranted(self) -> bool:
        return bool(isinstance(self.action, dict) and self.action.get("warranted"))

    @property
    def objective(self) -> str:
        if not isinstance(self.action, dict):
            return ""
        return str(self.action.get("objective") or "").strip()

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
        missing = (self.action or {}).get("missing") if isinstance(self.action, dict) else None
        if missing:
            lines.append("Still unknown: " + "; ".join(str(m) for m in missing))
        return "\n".join(lines)


class Planner:
    def __init__(self, config: Config, client: anthropic.Anthropic | None = None) -> None:
        self.config = config
        self.client = client or anthropic.Anthropic()
        # Only kinds that exist *and* are switched on. The intersection is taken
        # once, here, so the schema and the agent's tool agree on what is real.
        self.kinds = tuple(k for k in config.workers.kinds if registry.get(k))
        self.schema = build_schema(self.kinds)

    def _prompt(self, batch: Batch, previous: Plan | None, dossier: str = "") -> str:
        roster = ", ".join(batch.roster) or "unknown"
        parts = [
            f"Group: {batch.last.chat_label}",
            f"Members: {roster}",
        ]
        if dossier:
            parts.append("\n" + dossier)
        if previous:
            parts.append(
                "\nYour previous read (things may have moved on):\n" + previous.brief()
            )
        parts.append("\nNew messages:\n" + batch.transcript())
        return "\n".join(parts)

    def plan(
        self,
        batch: Batch,
        previous: Plan | None = None,
        dossier: str = "",
    ) -> Plan | None:
        """Produce a plan for this burst, or None if planning failed.

        Failure degrades to "no plan" rather than raising: a missing read costs
        the reply some context, while an exception here would dead-letter a
        message the agent was never going to answer anyway.

        ``dossier`` is what we already know about these people, so the model can
        answer "what is still missing" rather than re-deriving the whole picture
        from one burst.
        """
        workers = registry.menu(list(self.kinds)) or "- (none available)"
        try:
            response = self.client.messages.create(
                model=self.config.model_for("planner"),
                max_tokens=self.config.group.plan_max_tokens,
                system=SYSTEM.format(menu=personality.menu(), workers=workers),
                messages=[
                    {"role": "user", "content": self._prompt(batch, previous, dossier)}
                ],
                output_config={"format": {"type": "json_schema", "schema": self.schema}},
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
            facts=len(plan.facts_learned or []),
            action_kind=plan.action_kind, warranted=plan.action_warranted,

        )
        return plan
