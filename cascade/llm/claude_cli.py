"""Adapter for serving requests through the Claude Code CLI (ADR-0031). Pure.

The ``claude_code`` provider runs ``claude -p`` -- Claude Code's documented
headless mode -- on the operator's own machine, under their own Claude
subscription. It exists so the pipeline can produce real-model output before a
pay-as-you-go key is added. It is **not** the pinned configuration, and this
module is where the difference is made explicit rather than hidden:

* ``temperature`` and ``max_tokens`` cannot be set through the CLI. Every
  request in the cache key domain carries them, so a CLI response is not a
  response to the request the key describes. The provider therefore records
  under its own cache namespace (ADR-0031): its recordings can never be served
  as, or mistaken for, recordings made through the API.
* The CLI adds harness context of its own to every call. Measured on an
  isolated invocation: 448 input tokens for a ~45-token request.
* Extended thinking is on by default in the CLI and off on the API path; the
  environment built by :func:`cascade.config.claude_cli_environment` turns it
  off (measured: 235 thinking tokens with it, 0 without).

What *is* faithful: the model id, the system prompt (replaced, not appended),
the user message, and a forced tool's schema, which becomes ``--json-schema``
structured output and is returned as the ``tool_use`` block the caller parses.

Only one request shape exists in this codebase -- a system prompt, exactly one
user message with text content, and at most one forced tool -- so that is the
only shape accepted. Anything else is refused rather than flattened: rendering
a multi-turn conversation into one prompt would change what the model is
asked, which is the same defect as the ignored temperature, made silently.

No I/O here. ``cascade/llm/client.py`` runs the process, which keeps every
model call behind the one door (invariant 5) where it is cached, metered and
traced like any other.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from cascade.canonical import canonical_json
from cascade.llm.types import LLMError, LLMRequest

__all__ = [
    "ISOLATION_FLAGS",
    "UNCONTROLLED_FIELDS",
    "CliInvocation",
    "UnsupportedRequestShape",
    "build_invocation",
    "parse_cli_output",
    "to_messages_payload",
    "wire_schema",
]

# Request fields the CLI cannot honour. Reported by `cascade doctor` and in the
# ADR; the reason the provider has its own cache namespace.
UNCONTROLLED_FIELDS: tuple[str, ...] = ("max_tokens", "temperature")

# Each flag closes one channel through which something other than the request
# could reach the model. `--bare` would close more of them but reads only
# ANTHROPIC_API_KEY ("OAuth and keychain are never read"), which is the one
# credential this provider exists to avoid.
ISOLATION_FLAGS: tuple[str, ...] = (
    "--tools",
    "",  # no built-in tools: the model answers, it does not act
    "--strict-mcp-config",  # no MCP servers beyond --mcp-config, of which there are none
    "--setting-sources",
    "",  # no user, project or local settings files
    "--no-session-persistence",  # nothing written to the operator's session history
    "--output-format",
    "json",  # usage, cost and structured output in one parseable object
)


class UnsupportedRequestShape(LLMError):
    """A request the CLI cannot express without changing what is asked."""


@dataclass(frozen=True)
class CliInvocation:
    """One ``claude -p`` process: its arguments and what it reads on stdin."""

    argv: tuple[str, ...]
    stdin: str
    forced_tool: str | None


def _system_text(system: str | list[dict[str, Any]] | None) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    parts: list[str] = []
    for block in system:
        if block.get("type") != "text":
            raise UnsupportedRequestShape(
                f"system block of type {block.get('type')!r} has no CLI equivalent"
            )
        parts.append(str(block.get("text", "")))
    # Blank-line joined: the API renders consecutive system blocks as one
    # prompt, and the separator keeps the rules and the persona distinct.
    return "\n\n".join(parts)


# Keywords `claude -p`'s strict-mode validator refuses. Kept as a set rather
# than stripped inline so the next one found has an obvious home.
_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset({"discriminator"})


def wire_schema(schema: Any) -> Any:
    """Strip keywords the CLI's schema validator refuses. Pure.

    Measured, not guessed: `claude -p` rejects the agent's action tool with
    ``--json-schema is not a valid JSON Schema: strict mode: unknown keyword:
    "discriminator"``. Its validator runs in a strict mode that treats an
    unrecognised keyword as an error rather than ignoring it, and Pydantic
    emits ``discriminator`` for a tagged union -- which the action space is,
    over the seven §7.4 action types.

    This is why the compiler ran on a subscription and the agents could not:
    ``DRAFT_TOOL`` is a plain object and carries no discriminator, while
    ``ACTION_TOOL`` does. The failure was one call into the first step of the
    first run, having spent nothing, which is the one thing to be glad of.

    **Dropping it is lossless, and that is the whole argument.**
    ``discriminator`` is an OpenAPI dispatch hint layered on top of
    ``oneOf``: it tells a reader which branch to try first, and removes no
    constraint if absent, because every branch still carries its own ``type``
    literal and validation is unchanged. A model reading the stripped schema
    sees the same set of admissible actions.

    Applied at the wire boundary and nowhere else, on the ADR-0007 precedent
    that a provider-shaped rendering must not reach the cache key: the key is
    computed from the request, so two providers serving one request still
    resolve the same recording. ``claude_code`` additionally records in its
    own namespace (ADR-0031), so the stripped form cannot be served to an API
    provider even by accident.
    """
    if isinstance(schema, dict):
        return {
            key: wire_schema(value)
            for key, value in sorted(schema.items())
            if key not in _UNSUPPORTED_SCHEMA_KEYWORDS
        }
    if isinstance(schema, list):
        return [wire_schema(item) for item in schema]
    return schema


def build_invocation(request: LLMRequest, *, executable: str, model: str) -> CliInvocation:
    """The process that serves ``request`` through the CLI. Pure.

    Preserves the invariant that nothing reaches the model except the request
    itself: the system prompt replaces the CLI's own, tools are disabled, and
    settings and MCP servers are not loaded. Raises
    :class:`UnsupportedRequestShape` for anything that cannot be expressed
    faithfully.
    """
    if len(request.messages) != 1 or request.messages[0].get("role") != "user":
        raise UnsupportedRequestShape(
            f"the CLI takes exactly one user message; this request has "
            f"{len(request.messages)} message(s). Rendering a conversation into one "
            "prompt would change what the model is asked."
        )
    content = request.messages[0].get("content")
    if not isinstance(content, str):
        raise UnsupportedRequestShape(
            "the CLI takes text content only; this message carries content blocks"
        )

    argv: list[str] = [executable, "-p", "--model", model]
    system = _system_text(request.system)
    if system:
        argv += ["--system-prompt", system]

    forced: str | None = None
    if request.tools:
        choice = request.tool_choice or {}
        if choice.get("type") != "tool" or len(request.tools) != 1:
            raise UnsupportedRequestShape(
                "the CLI can enforce exactly one named tool, as structured output; "
                f"got {len(request.tools)} tool(s) with tool_choice={choice!r}"
            )
        tool = request.tools[0]
        forced = str(tool["name"])
        if choice.get("name") != forced:
            raise UnsupportedRequestShape(
                f"tool_choice names {choice.get('name')!r} but the only tool is {forced!r}"
            )
        argv += ["--json-schema", canonical_json(wire_schema(tool["input_schema"]))]

    argv += list(ISOLATION_FLAGS)
    return CliInvocation(argv=tuple(argv), stdin=content, forced_tool=forced)


def to_messages_payload(
    cli: dict[str, Any], *, invocation: CliInvocation, logical_model: str
) -> dict[str, Any]:
    """Project the CLI's JSON result onto a Messages API response body. Pure.

    Preserves the invariant that every consumer downstream of the client --
    replay, the cost meter, the compiler's and the agent's tool parsing -- sees
    one response shape whichever provider served it. A forced tool's
    structured output becomes the ``tool_use`` block the API would have
    returned. The CLI's own accounting is kept under ``claude_code`` so a
    recording states how it was made.
    """
    if cli.get("is_error"):
        raise LLMError(
            "claude -p reported an error: "
            f"subtype={cli.get('subtype')!r} status={cli.get('api_error_status')!r} "
            f"result={str(cli.get('result', ''))[:300]!r}"
        )
    usage = cli.get("usage")
    if not isinstance(usage, dict) or "input_tokens" not in usage:
        raise LLMError("claude -p returned no usage block; the call cannot be accounted for")

    if invocation.forced_tool is not None:
        structured = cli.get("structured_output")
        if not isinstance(structured, dict):
            raise LLMError(
                f"claude -p was asked for structured output for {invocation.forced_tool!r} "
                "and returned none"
            )
        digest = hashlib.sha256(canonical_json(structured).encode("utf-8")).hexdigest()
        content: list[dict[str, Any]] = [
            {
                "type": "tool_use",
                "id": f"toolu_cli_{digest[:24]}",
                "name": invocation.forced_tool,
                "input": structured,
            }
        ]
        stop_reason = "tool_use"
    else:
        content = [{"type": "text", "text": str(cli.get("result", ""))}]
        stop_reason = str(cli.get("stop_reason") or "end_turn")

    return {
        "id": f"msg_cli_{cli.get('uuid', '')}",
        "type": "message",
        "role": "assistant",
        "model": logical_model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage["input_tokens"]),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
            "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
        },
        "claude_code": {
            # What the CLI says the call would have cost at list price. Not
            # booked: a subscription call bills no tokens (ADR-0031).
            "notional_cost_usd": cli.get("total_cost_usd"),
            "num_turns": cli.get("num_turns"),
            "duration_api_ms": cli.get("duration_api_ms"),
        },
    }


def parse_cli_output(stdout: str) -> dict[str, Any]:
    """Decode the CLI's single JSON result object. Pure."""
    try:
        decoded = json.loads(stdout)
    except ValueError as exc:
        raise LLMError(f"claude -p did not return JSON: {stdout[:300]!r}") from exc
    if not isinstance(decoded, dict):
        raise LLMError(f"claude -p returned {type(decoded).__name__}, not an object")
    return decoded
