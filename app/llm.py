"""Unified LLM backend -- this is what "run natively in Claude Code" actually means: prefer
calling out to the user's OWN already-authenticated `claude` CLI (via claude-agent-sdk) so Stage 2
/ Stage 3 run on their enterprise Claude Code seat with no separate Anthropic API key at all, and
fall back to the direct Anthropic API (app/claude_client.py, needs ANTHROPIC_API_KEY) only when
native isn't available -- e.g. once this is actually deployed somewhere Claude Code itself doesn't
run (a Tines-called hosted service).

LLM_BACKEND env var: "auto" (default, try native then fall back), "native" (native only, raise if
unavailable), "api" (skip native, go straight to the API-key path).

IMPORTANT on cli_path resolution: claude-agent-sdk's own default prefers ITS bundled claude.exe
over anything on PATH. That bundled copy is very likely NOT the same login as the user's normal
Claude Code installation, which is the whole point of this module (use THEIR enterprise seat). So
this module explicitly looks for a `claude` on PATH first and only lets the SDK fall back to its
bundled copy if that search comes up empty.
"""

import os
import pathlib
import shutil

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        CLINotFoundError,
        ResultMessage,
        TextBlock,
        query,
    )
    NATIVE_SDK_AVAILABLE = True
except ImportError:
    NATIVE_SDK_AVAILABLE = False

from . import claude_client
from .prompts import PROPOSE_VARIABLE_CARD_TOOL

BACKEND = os.environ.get("LLM_BACKEND", "auto")


_KNOWN_INSTALL_LOCATIONS = [
    # The native installer's own default (irm https://claude.ai/install.ps1 | iex on Windows,
    # curl -fsSL https://claude.ai/install.sh | bash on macOS/Linux). shutil.which() alone isn't
    # reliable here: the process running this app (e.g. this Cowork/agent-mode session's own
    # shell) can have a completely different PATH than the terminal the user actually ran
    # `claude login` from, even on the same machine -- checking the install path directly sidesteps
    # that instead of silently falling back to the SDK's bundled (unauthenticated) copy.
    pathlib.Path.home() / ".local" / "bin" / "claude.exe",
    pathlib.Path.home() / ".local" / "bin" / "claude",
]


def _resolve_cli_path():
    """None means "let the SDK decide" (its own bundled-first default). Returning a real path
    here means "use THIS one" -- see module docstring for why that matters."""
    found = shutil.which("claude") or shutil.which("claude.exe")
    if found:
        return found
    for candidate in _KNOWN_INSTALL_LOCATIONS:
        if candidate.exists():
            return str(candidate)
    return None


class NativeUnavailable(Exception):
    pass


async def _native_run(prompt_text, system_prompt, resume_session_id=None, output_schema=None):
    if not NATIVE_SDK_AVAILABLE:
        raise NativeUnavailable("claude-agent-sdk is not installed")

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        allowed_tools=[],  # pure completion -- no file/bash/edit access for this call
        cli_path=_resolve_cli_path(),
        resume=resume_session_id,
        max_turns=1,
        output_format={"type": "json_schema", "schema": output_schema} if output_schema else None,
    )

    text_parts = []
    structured_output = None
    session_id = None
    error_result = None
    try:
        async for msg in query(prompt=prompt_text, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
            elif isinstance(msg, ResultMessage):
                session_id = msg.session_id
                structured_output = msg.structured_output
                if msg.is_error:
                    error_result = msg.result
                    break  # let the async generator close cleanly before we raise
    except CLINotFoundError as exc:
        raise NativeUnavailable(str(exc))

    if error_result is not None:
        raise RuntimeError(f"Claude Code run failed: {error_result}")

    return {"text": "".join(text_parts), "structured_output": structured_output, "session_id": session_id}


async def extract_structured(system_prompt, user_content, tool):
    """Generic structured-output call: native (Claude Code session, json_schema output_format)
    first, falling back to the direct API's forced tool_choice. `tool` is a Messages-API-shaped
    tool dict ({"name", "description", "input_schema"}) -- both backends consume its input_schema
    as the target JSON schema, so callers write ONE schema, not one per backend. Used by both
    Stage 2 (propose) and the base-case-financials extraction -- same mechanism, different schema."""
    schema = tool["input_schema"]

    if BACKEND in ("auto", "native"):
        try:
            result = await _native_run(user_content, system_prompt, output_schema=schema)
            if result["structured_output"] is None:
                raise RuntimeError("native run completed but returned no structured_output")
            return result["structured_output"], result["session_id"]
        except NativeUnavailable:
            if BACKEND == "native":
                raise
        except Exception:
            if BACKEND == "native":
                raise
            # fall through to API backend below rather than surface a native-only stack trace

    import anyio

    parsed = await anyio.to_thread.run_sync(
        claude_client.call_with_tool, system_prompt, [{"role": "user", "content": user_content}], tool
    )
    return parsed, None


async def propose(system_prompt, user_content):
    """Stage 2. Returns the parsed variable-card dict, native session_id (or None if the api
    fallback was used, which has no session concept the same way)."""
    return await extract_structured(system_prompt, user_content, PROPOSE_VARIABLE_CARD_TOOL)


async def chat_structured(system_prompt, chat_history, tool, native_session_id=None):
    """Stage 3, tool-augmented: the same conversational continuity as chat() below (native session
    resume, last-turn-only for native / full history for the API fallback), but the reply comes
    back as structured output via `tool` (a {"reply": str, "proposed_change": {...}|null} shape)
    instead of plain text -- so the frontend can render an optional Confirm/Deny card alongside
    the chat bubble. A straight merge of chat()'s continuity logic and extract_structured()'s
    structured-output mechanism; _native_run already accepts resume_session_id and output_schema
    together, so no changes needed there."""
    last_user_message = chat_history[-1]["content"]
    schema = tool["input_schema"]

    if BACKEND in ("auto", "native"):
        try:
            result = await _native_run(last_user_message, system_prompt, resume_session_id=native_session_id, output_schema=schema)
            if result["structured_output"] is None:
                raise RuntimeError("native run completed but returned no structured_output")
            return result["structured_output"], result["session_id"]
        except NativeUnavailable:
            if BACKEND == "native":
                raise
        except Exception:
            if BACKEND == "native":
                raise

    import anyio

    parsed = await anyio.to_thread.run_sync(claude_client.call_with_tool, system_prompt, chat_history, tool)
    return parsed, None


async def chat(system_prompt, chat_history, native_session_id=None):
    """Stage 3. chat_history: full list of {"role","content"} turns so far (used by the API
    fallback, which is stateless). native_session_id: if set and the native backend is used, only
    the LAST user turn is sent -- Claude Code's own `resume` mechanic supplies the rest, matching
    how a real Claude Code session actually keeps context."""
    last_user_message = chat_history[-1]["content"]

    if BACKEND in ("auto", "native"):
        try:
            result = await _native_run(last_user_message, system_prompt, resume_session_id=native_session_id)
            return result["text"], result["session_id"]
        except NativeUnavailable:
            if BACKEND == "native":
                raise
        except Exception:
            if BACKEND == "native":
                raise

    import anyio

    reply = await anyio.to_thread.run_sync(claude_client.call_chat, system_prompt, chat_history)
    return reply, None
