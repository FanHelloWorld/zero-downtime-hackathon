"""Bright Data's hosted MCP server, wired through Anthropic's MCP connector.

The connector runs server-side: Anthropic holds the connection to Bright Data and
executes the tool calls, returning ``mcp_tool_use`` / ``mcp_tool_result`` blocks
in the same response. Nothing MCP-shaped runs on this machine — no client, no
``npx``, no subprocess, no second terminal. That is the whole reason a worker can
do real web research from inside the supervisor process.

Two things here are safety, not plumbing:

* **The toolset is an allowlist.** Bright Data exposes 60+ tools including
  browser automation. ``default_config.enabled: false`` turns everything off,
  then the configured names are turned back on individually.
* **The URL is a secret.** This server authenticates by query token, so the
  assembled URL carries the credential. It is never logged — ``redact`` exists
  for that and every event in this package uses it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from plug.config import BrightDataConfig

BETA = "mcp-client-2025-11-20"
SERVER_NAME = "brightdata"


@dataclass(frozen=True, slots=True)
class MCPWiring:
    """The request fields that attach an MCP server to one Messages call."""

    url: str
    tools: tuple[str, ...]

    def request_kwargs(self) -> dict:
        """Everything ``messages.create`` needs, ready to splat.

        Both halves are required: an ``mcp_servers`` entry with no matching
        toolset is rejected as a validation error, and every server must be
        referenced by exactly one toolset.
        """
        return {
            "betas": [BETA],
            "mcp_servers": [
                {"type": "url", "url": self.url, "name": SERVER_NAME},
            ],
            "tools": [
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": SERVER_NAME,
                    "default_config": {"enabled": False},
                    "configs": {name: {"enabled": True} for name in self.tools},
                }
            ],
        }


def redact(url: str) -> str:
    """A loggable form of the endpoint, with the token removed."""
    parts = urlsplit(url)
    query = [(k, "***" if k.lower() == "token" else v) for k, v in parse_qsl(parts.query)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def wiring(config: BrightDataConfig) -> MCPWiring | None:
    """Assemble the connection, or None when it isn't usable.

    Returns None rather than raising when the token is missing: a worker without
    web access should fall back to what the model already knows and say so in the
    log, not dead-letter a job and leave the chat hanging.
    """
    if not config.enabled:
        return None
    token = os.environ.get(config.token_env, "").strip()
    if not token:
        return None
    if not config.tools:
        return None

    parts = urlsplit(config.url)
    query = dict(parse_qsl(parts.query))
    query["token"] = token
    if config.groups:
        # Beyond the always-on core tools, a tool must be in a loaded group
        # before the allowlist can enable it.
        query["groups"] = ",".join(config.groups)

    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    return MCPWiring(url=url, tools=tuple(config.tools))
