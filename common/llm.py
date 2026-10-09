"""The only place that talks to the model.

Kept tiny and separate so that every call site is visible in one `grep`: in
this lab every model call goes through `call`, and `CALLS` counts them.
"""

from __future__ import annotations

import json
import os

import litellm

from common.tools import SENTINEL

MAX_TOKENS = 2048  # tool calls get truncated below this on some gateways

# Every call to the model, counted: the slides compare steps by calls per investigation.
CALLS = 0


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
    global CALLS
    CALLS += 1
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
        # A local server (LM Studio, Ollama, vLLM) needs no key, but the OpenAI
        # client refuses to send a request without one: any string will do.
        kwargs["api_key"] = os.environ.get("LLM_API_KEY") or "no-key"
    return litellm.completion(**kwargs)


def tool_call(response) -> tuple[str, dict]:
    """Extract (tool name, arguments) from a response, or raise NoToolCall."""
    message = response.choices[0].message
    calls = getattr(message, "tool_calls", None)
    if not calls:
        raise NoToolCall(f"expected a tool call, got prose: {str(message.content)[:200]}")
    # Not always calls[0]: a model that rambles mid-call can emit a first
    # fragment with no name at all, and taking it blindly turned a correct
    # tool choice into "unknown tool: None".
    fn = next((c.function for c in calls if getattr(c.function, "name", None)), None)
    if fn is None:
        raise NoToolCall("the model returned a tool call with no tool name")

    # The sentinel takes no parameters, so its arguments cannot be wrong — and
    # this model sometimes fills them with a runaway apology. Judging the
    # decision by its punctuation would discard a correct refusal.
    if fn.name == SENTINEL:
        return fn.name, {}

    try:
        args = json.loads(fn.arguments or "{}")
    except ValueError as exc:
        raise NoToolCall(f"tool arguments were not valid JSON: {fn.arguments[:200]}") from exc
    return fn.name, args


RETRY_NUDGE = ("\nYour previous reply was not a valid tool call. Answer with one tool "
               "call and nothing else.")


def call_tool(messages: list[dict], tools: list[dict], model: str | None = None,
              attempts: int = 2) -> tuple[str, dict]:
    """Ask for a tool call, drawing again if the envelope comes back malformed.

    A malformed reply is a bad envelope, not a refusal: this model sometimes
    returns arguments that are not JSON, or a first tool call with no name.
    Redrawing costs one call and usually comes back clean, where giving up
    costs the whole answer.

    Two attempts, then raise. A model that cannot do constrained tool calling
    is a measurement worth surfacing, and retrying forever would hide it.
    """
    failures = []
    for attempt in range(attempts):
        drafts = messages
        if attempt:
            drafts = [dict(m) for m in messages]
            drafts[0]["content"] = str(drafts[0].get("content", "")) + RETRY_NUDGE
        try:
            return tool_call(call(drafts, tools=tools, model=model))
        except NoToolCall as exc:
            failures.append(str(exc))
    raise NoToolCall(" | ".join(failures))


def text(response) -> str:
    return (response.choices[0].message.content or "").strip()


if __name__ == "__main__":
    # Run with: .venv/bin/python -m common.llm
    # Every case below is a real reply this model produced against the live
    # stack — the malformed ones cost an afternoon of "something went wrong".
    from types import SimpleNamespace as N

    def reply(*calls, content=""):
        made = [N(function=N(name=n, arguments=a)) for n, a in calls]
        return N(choices=[N(message=N(tool_calls=made or None, content=content))])

    assert tool_call(reply(("loki_query", '{"services": ["store"]}'))) == \
        ("loki_query", {"services": ["store"]})

    # A first fragment with no name at all, followed by the real choice.
    assert tool_call(reply((None, "{}"), ("loki_query", "{}")))[0] == "loki_query"

    # The sentinel takes no parameters, so a runaway apology in its arguments
    # is noise, not a wrong decision.
    assert tool_call(reply((SENTINEL, "{}'} There is no tool. I'm sorry. " * 50))) == \
        (SENTINEL, {})

    for bad, why in [(reply(content="I think you should check Loki."), "prose"),
                     (reply((None, "{}")), "no name anywhere"),
                     (reply(("loki_query", "not json at all")), "unparsable arguments")]:
        try:
            tool_call(bad)
        except NoToolCall:
            pass
        else:
            raise AssertionError(f"should have raised NoToolCall: {why}")

    print("llm.tool_call: ok")

    # call_tool draws again when the envelope is malformed, and gives up after
    # the second attempt rather than hammering a model that cannot comply.
    seen = []

    def draws(*replies):
        def fake(messages, tools=None, model=None):
            seen.append(messages[0]["content"])
            return replies[min(len(seen) - 1, len(replies) - 1)]
        return fake

    call = draws(reply(("loki_query", "not json")), reply(("loki_query", '{"services": []}')))
    assert call_tool([{"role": "system", "content": "pick one"}], []) == \
        ("loki_query", {"services": []})
    assert len(seen) == 2, "a malformed reply should be redrawn once"
    assert RETRY_NUDGE in seen[1], "the second draw should say the first was malformed"
    assert RETRY_NUDGE not in seen[0]

    seen.clear()
    call = draws(reply(content="I really think you should check Loki."))
    try:
        call_tool([{"role": "system", "content": "pick one"}], [])
    except NoToolCall:
        assert len(seen) == 2, "two attempts, then stop"
    else:
        raise AssertionError("a model that never emits a tool call must surface, not loop")

    print("llm.call_tool: ok")
