"""The only place that talks to the model.

Kept tiny and separate so that every call site is visible in one `grep`: in
this lab the model is called in exactly three places, and being able to prove
it is part of the argument.
"""

from __future__ import annotations

import json
import os

import litellm

MAX_TOKENS = 2048  # tool calls get truncated below this on some gateways


class NoToolCall(Exception):
    """The model answered prose where a tool call was required.

    This is a measurement, not a bug: some models simply cannot do constrained
    tool calling, and the honest thing is to stop rather than to guess.
    """


def call(messages: list[dict], tools: list[dict] | None = None, model: str | None = None):
    """One call, with the model name resolved the way LiteLLM expects.

    Two routes, and mixing them up costs an afternoon:

    * ``provider/model`` (e.g. ``mistral/codestral-2508``) — LiteLLM routes to
      that provider natively and reads its own key from the environment. Do
      **not** pass a custom ``api_base`` here, or the request goes to your
      gateway asking for a model it has never heard of.
    * a bare name (e.g. ``my-model``) — treated as an OpenAI-compatible
      endpoint: prefixed with ``openai/`` and sent to ``LLM_BASE_URL`` with
      ``LLM_API_KEY``. This is how you point the lab at a gateway or a local
      server.
    """
    name = model or os.environ.get("AGENT_MODEL", "mistral/codestral-2508")
    namespaced = "/" in name

    kwargs = {
        "model": name if namespaced else f"openai/{name}",
        "messages": messages,
        "max_tokens": MAX_TOKENS,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "required"  # no prose when a tool is expected
    if not namespaced:
        if os.environ.get("LLM_BASE_URL"):
            kwargs["api_base"] = os.environ["LLM_BASE_URL"]
        if os.environ.get("LLM_API_KEY"):
            kwargs["api_key"] = os.environ["LLM_API_KEY"]
    return litellm.completion(**kwargs)


def tool_call(response) -> tuple[str, dict]:
    """Extract (tool name, arguments) from a response, or raise NoToolCall."""
    message = response.choices[0].message
    calls = getattr(message, "tool_calls", None)
    if not calls:
        raise NoToolCall(f"expected a tool call, got prose: {str(message.content)[:200]}")
    fn = calls[0].function
    try:
        args = json.loads(fn.arguments or "{}")
    except ValueError as exc:
        raise NoToolCall(f"tool arguments were not valid JSON: {fn.arguments[:200]}") from exc
    return fn.name, args


def text(response) -> str:
    return (response.choices[0].message.content or "").strip()
