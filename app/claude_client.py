"""Thin wrapper around the Anthropic API -- the "intelligent backend" for Stage 2 (first-pass
proposal) and Stage 3 (challenge conversation), replacing what used to run as Claude Code's own
model loop inside the /monte-carlo-model skill.

Reads the API key from the ANTHROPIC_API_KEY environment variable -- either already set in the
shell, or loaded from a local .env file (never committed; see .env.example at the repo root).
Never hardcode a key here or accept one over the wire from the frontend.
"""

import os

import anthropic
from dotenv import load_dotenv

load_dotenv()  # no-op if there's no .env file; real shell env vars always take precedence

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")


def _client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Set it as an environment variable before starting the "
            "app -- see README.md. The propose/chat endpoints cannot call Claude without it."
        )
    return anthropic.Anthropic(api_key=api_key)


def call_with_tool(system_prompt, messages, tool):
    """Force a single structured tool call and return its parsed input. Used for Stage 2's
    proposal, where we need reliable JSON back, not prose Claude might format inconsistently."""
    client = _client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=system_prompt,
        messages=messages,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == tool["name"]:
            return block.input
    raise RuntimeError("Claude did not return the expected tool call")


def call_chat(system_prompt, messages):
    """Plain conversational turn for Stage 3 -- returns assistant text."""
    client = _client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=system_prompt,
        messages=messages,
    )
    return "".join(block.text for block in response.content if block.type == "text")
