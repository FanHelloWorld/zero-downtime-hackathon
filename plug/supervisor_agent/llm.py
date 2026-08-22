"""Which model plays which role, behind one small interface.

A seam, not a framework. Every plain text-in/text-out call in the supervisor goes
through ``TextModel``, so moving a role to another provider — Gemini Flash is the
one on the table — is a config line plus one adapter class, not a refactor.

Two roles deliberately do not come through here:

* the **reply** loop, which needs the Anthropic tool runner and closure-bound
  tools, and
* **worker research**, which needs the MCP connector. That connector is an
  Anthropic API feature: Anthropic's servers hold the connection to the MCP
  server. A Gemini research step would need a real MCP client running in this
  process, which is exactly the dependency the current design avoids.

So the roles that can move cheaply are ``planner`` and ``worker_compose``.
"""

from __future__ import annotations

from typing import Protocol

import anthropic

from plug.config import Config


class TextModel(Protocol):
    """One prompt in, one string out. No tools, no state."""

    name: str

    def complete(self, *, system: str, prompt: str, max_tokens: int) -> str: ...


class AnthropicText:
    """``TextModel`` over the Messages API."""

    def __init__(self, model: str, client: anthropic.Anthropic | None = None) -> None:
        self.name = model
        self.client = client or anthropic.Anthropic()

    def complete(self, *, system: str, prompt: str, max_tokens: int) -> str:
        response = self.client.messages.create(
            model=self.name,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        # Join rather than take the first: a response can arrive as several text
        # blocks, and dropping all but one silently truncates the reply.
        return "\n".join(b.text for b in response.content if b.type == "text").strip()


def for_role(
    config: Config, role: str, client: anthropic.Anthropic | None = None
) -> TextModel:
    """The model configured for ``role``, falling back to the top-level ``model``."""
    return AnthropicText(config.model_for(role), client)
