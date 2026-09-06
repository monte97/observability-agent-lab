"""The diagnostic loop: hypotheses -> investigate -> weigh, and stop when it
makes sense to stop.

Six nodes, and the model is called in exactly three of them. Everything else —
discovery, query composition, execution, and the decision to keep going — is
plain code.

    discover -> hypotheses -> choose -> execute -> weigh -+-> synthesize
                                 ^                        |
                                 +------------------------+

What makes this different from a `for` loop around a chat call is the exit
condition: the loop stops when the evidence says so (a hypothesis crossed the
confidence threshold) or when the budget runs out — not after a fixed number
of turns.
"""

from __future__ import annotations

import copy
import json
from typing import TypedDict

from langgraph.graph import END, StateGraph

import backends
import discovery
import llm
import tools

CONFIDENCE_THRESHOLD = 0.7
MAX_CYCLES = 3


class State(TypedDict, total=False):
    """Shared state. `total=False` on purpose: every node returns only the
    fields it actually produced, and LangGraph merges them. No node has to
    rebuild the whole state, which is what keeps the nodes composable."""
    question: str
    direct: bool            # <- caller: the question IS the hypothesis
    topology: dict          # <- discover
    hypotheses: list[dict]  # <- hypotheses / weigh
    choice: dict            # <- choose
    findings: list[dict]    # <- execute (append-only)
    facts: str              # <- process
    answer: str             # <- synthesize
    error: str
    stop: bool
    cycle: int


# --------------------------------------------------------------------------
# discover — no model involved
# --------------------------------------------------------------------------

def node_discover(state: State, start: int, end: int) -> dict:
    return {"topology": discovery.topology(start, end)}


# --------------------------------------------------------------------------
# hypotheses — model call 1 of 3
# --------------------------------------------------------------------------

def _hypotheses_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "propose_hypotheses",
            "description": "Propose 2 to 4 candidate causes, each with a plan to check it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hypotheses": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "The candidate cause."},
                                "plan": {"type": "string", "description": "How to check it."},
                            },
                            "required": ["text", "plan"],
                        },
                    }
                },
                "required": ["hypotheses"],
            },
        },
    }


def _topology_summary(topology: dict) -> str:
    metrics = topology.get("metrics") or []
    return (
        f"Services: {', '.join(topology.get('services') or []) or '(none found)'}\n"
        f"Sample of available metrics: {', '.join(metrics[:15]) or '(none found)'}"
    )


def node_hypotheses(state: State, model: str | None = None) -> dict:
    """Ask the model for candidate causes, anchored to names that exist.

    In **direct mode** this node does not call the model at all: the question
    *is* the hypothesis. Worth knowing because it changes what the loop proves
    — direct mode shows that the agent can answer, scenario mode shows that it
    can reason. A direct question ("which log lines did X produce?") sent
    through scenario mode makes the model invent causes nobody asked about.

    The topology goes in the prompt so the hypotheses talk about real services.
    Note that this is *not* what stops the model from hallucinating a name —
    that job belongs to the enum in the tool schema. Prompting helps; only the
    schema guarantees.
    """
    if state.get("direct"):
        return {"hypotheses": [{"text": state["question"], "plan": "check it directly",
                                "confidence": 0.5, "evidence": None}]}

    response = llm.call(
        [
            {"role": "system", "content":
                "You are an SRE triage agent. From the question and the topology, propose "
                "2-4 plausible causes, each with a short plan to verify it. Use the real "
                "service names you are given."},
            {"role": "user", "content":
                f"Question: {state['question']}\n\n{_topology_summary(state['topology'])}"},
        ],
        tools=[_hypotheses_schema()],
        model=model,
    )
    _, args = llm.tool_call(response)
    hypotheses = args.get("hypotheses") or []
    for h in hypotheses:
        h["confidence"] = 0.5
        h["evidence"] = None
    return {"hypotheses": hypotheses}


# --------------------------------------------------------------------------
# choose — model call 2 of 3
# --------------------------------------------------------------------------

def node_choose(state: State, model: str | None = None) -> dict:
    """Pick the most promising hypothesis, then let the model pick the tool.

    Which hypothesis to investigate is decided by code (highest confidence,
    preferring one never investigated yet). Which tool answers it is decided by
    the model — from a menu whose values were discovered a moment ago.
    """
    hypotheses = state.get("hypotheses") or []
    if not hypotheses:
        return {"error": "no hypothesis to investigate", "stop": True}

    candidates = [h for h in hypotheses if not h.get("discarded")] or hypotheses
    target = max(candidates, key=lambda h: (h.get("confidence", 0), h.get("evidence") is None))
    index = hypotheses.index(target)

    hint = ""
    last = (state.get("findings") or [])[-1:]
    if last and last[0].get("hits") == 0 and last[0].get("tool") != tools.SENTINEL:
        hint = (f"\nThe previous attempt used {last[0]['tool']} and returned nothing "
                f"({last[0].get('query')}). Try a different angle.")

    try:
        response = llm.call(
            [
                {"role": "system", "content":
                    "You are an SRE triage agent. Pick exactly one tool to check the "
                    "hypothesis. If no tool can answer it, pick the sentinel tool." + hint},
                {"role": "user", "content": target["text"]},
            ],
            tools=tools.schemas(state["topology"]),
            model=model,
        )
        name, args = llm.tool_call(response)
    except llm.NoToolCall as exc:
        return {"error": str(exc), "stop": True}

    return {"choice": {"tool": name, "arguments": args, "hypothesis_index": index}}


# --------------------------------------------------------------------------
# execute + process — no model involved
# --------------------------------------------------------------------------

def node_execute(state: State, start: int, end: int) -> dict:
    """Compose the query from the chosen parameters and run it.

    This is where the second half of the argument lives: the query string is
    built here, by code, from values the model could only pick out of an enum.
    """
    choice = state.get("choice") or {}
    tool, args = choice.get("tool"), choice.get("arguments") or {}

    if tool == tools.SENTINEL:
        result = {"ok": True, "hits": 0, "sample": [], "query": None, "sentinel": True}
    elif tool == "loki_query":
        query = tools.loki_compose(args.get("services") or [], args.get("level"))
        result = backends.run_loki(query, start, end)
    elif tool == "mimir_query":
        query = tools.mimir_compose(args.get("metric"), args.get("label_filters"),
                                    args.get("rate_window"))
        result = backends.run_mimir(query, start, end)
    else:
        return {"error": f"unknown tool: {tool}", "stop": True}

    finding = {"tool": tool, **result}
    findings = list(state.get("findings") or []) + [finding]

    out: dict = {"findings": findings}
    index = choice.get("hypothesis_index")
    hypotheses = state.get("hypotheses") or []
    # Only a finding with data becomes evidence: a failed attempt is not a source.
    if index is not None and 0 <= index < len(hypotheses) and result.get("hits"):
        updated = copy.deepcopy(hypotheses)
        updated[index]["evidence"] = result["query"]
        out["hypotheses"] = updated
    return out


def node_process(state: State) -> dict:
    findings = state.get("findings") or []
    if not findings:
        return {"facts": "(no data)"}
    last = findings[-1]
    if last.get("sentinel"):
        return {"facts": "no tool of mine covers this question"}
    return {"facts": backends.to_facts(last, last["tool"])}


# --------------------------------------------------------------------------
# weigh — model call 3 of 3, plus the deterministic guardrail
# --------------------------------------------------------------------------

def _weigh_schema(count: int) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "score_hypotheses",
            "description": "Give each hypothesis a confidence between 0 and 1.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scores": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "index": {"type": "integer",
                                          "description": f"0 to {max(count - 1, 0)}"},
                                "confidence": {"type": "number"},
                            },
                            "required": ["index", "confidence"],
                        },
                    }
                },
                "required": ["scores"],
            },
        },
    }


def node_weigh(state: State, model: str | None = None) -> dict:
    """The model scores; then the code overrules it where the evidence is empty.

    This is the shape worth stealing from this lab: the model proposes, the
    code disposes. A hypothesis whose own query came back empty cannot stay the
    conclusion just because the model liked it — no matter how confident the
    model sounded.
    """
    hypotheses = copy.deepcopy(state.get("hypotheses") or [])
    listing = "\n".join(f"{i}) {h['text']}" for i, h in enumerate(hypotheses))

    response = llm.call(
        [
            {"role": "system", "content":
                "You are an SRE triage agent. Score each hypothesis from 0 to 1 based on the "
                "observed facts. High confidence means the facts support it."},
            {"role": "user", "content":
                f"Question: {state['question']}\n\nObserved: {state.get('facts', '')}\n\n"
                f"Hypotheses:\n{listing}"},
        ],
        tools=[_weigh_schema(len(hypotheses))],
        model=model,
    )
    _, args = llm.tool_call(response)
    for score in args.get("scores") or []:
        i = score.get("index")
        if isinstance(i, int) and 0 <= i < len(hypotheses):
            hypotheses[i]["confidence"] = score.get("confidence", 0)

    # --- the deterministic guardrail -------------------------------------
    # If the query that was supposed to prove this hypothesis returned nothing,
    # the hypothesis is discarded regardless of the score. Without this, the
    # loop concludes on hypotheses that were never actually supported.
    findings = state.get("findings") or []
    last = findings[-1] if findings else {}
    empty_evidence = last and last.get("hits") == 0 and not last.get("sentinel")
    index = (state.get("choice") or {}).get("hypothesis_index")
    if empty_evidence and index is not None and 0 <= index < len(hypotheses):
        hypotheses[index]["confidence"] = 0
        hypotheses[index]["discarded"] = True

    cycle = (state.get("cycle") or 0) + 1
    all_out = bool(hypotheses) and all(h.get("discarded") for h in hypotheses)
    return {"hypotheses": hypotheses, "cycle": cycle, "stop": all_out and cycle >= MAX_CYCLES}


def route_after_weigh(state: State) -> str:
    """Keep going, or answer? The condition is about evidence, not about turns."""
    if state.get("stop") or state.get("error"):
        return "synthesize"
    if (state.get("cycle") or 0) >= MAX_CYCLES:
        return "synthesize"

    findings = state.get("findings") or []
    last = findings[-1] if findings else {}
    # An empty result is not a conclusion: spend another cycle if budget allows.
    if last and last.get("hits") == 0 and not last.get("sentinel"):
        return "choose"

    best = max((h.get("confidence", 0) for h in state.get("hypotheses") or []), default=0)
    return "synthesize" if best >= CONFIDENCE_THRESHOLD else "choose"


# --------------------------------------------------------------------------
# synthesize — fixed answers where the code already knows what to say
# --------------------------------------------------------------------------

def node_synthesize(state: State, model: str | None = None) -> dict:
    if state.get("error"):
        return {"answer": "I could not investigate: something went wrong while exploring "
                          "the data.", "stop": True}

    findings = state.get("findings") or []
    if findings and findings[-1].get("sentinel"):
        return {"answer": "No tool of mine covers this question — it goes beyond what this "
                          "stack exposes.", "stop": True}

    active = [h for h in (state.get("hypotheses") or []) if not h.get("discarded")]
    best = max((h.get("confidence", 0) for h in active), default=0)
    if state.get("hypotheses") and best < CONFIDENCE_THRESHOLD:
        # Saying "I do not know" is a valid triage answer. Inventing a cause is not.
        lines = [f"- {h['text']} (confidence {h.get('confidence', 0)})" for h in active]
        return {
            "answer": "Inconclusive: no hypothesis reached the confidence threshold "
                      f"({CONFIDENCE_THRESHOLD}). Still open:\n" + "\n".join(lines or ["(all discarded)"]),
            "stop": True,
        }

    response = llm.call(
        [
            {"role": "system", "content":
                "You are an SRE triage analyst. In 2-4 sentences, state the most likely cause "
                "and cite the query the finding came from. Do not invent numbers."},
            {"role": "user", "content":
                f"Question: {state['question']}\nObserved: {state.get('facts', '')}\n"
                f"Source: {(findings[-1] if findings else {}).get('query')}"},
        ],
        model=model,
    )
    return {"answer": llm.text(response), "stop": True}


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------

def build_graph(start: int, end: int, model: str | None = None):
    graph = StateGraph(State)
    graph.add_node("discover", lambda s: node_discover(s, start, end))
    graph.add_node("hypotheses", lambda s: node_hypotheses(s, model))
    graph.add_node("choose", lambda s: node_choose(s, model))
    graph.add_node("execute", lambda s: node_execute(s, start, end))
    graph.add_node("process", node_process)
    graph.add_node("weigh", lambda s: node_weigh(s, model))
    graph.add_node("synthesize", lambda s: node_synthesize(s, model))

    graph.set_entry_point("discover")
    graph.add_edge("discover", "hypotheses")
    graph.add_edge("hypotheses", "choose")
    graph.add_edge("choose", "execute")
    graph.add_edge("execute", "process")
    graph.add_edge("process", "weigh")
    graph.add_conditional_edges("weigh", route_after_weigh,
                                {"choose": "choose", "synthesize": "synthesize"})
    graph.add_edge("synthesize", END)
    return graph.compile()


def run(question: str, start: int, end: int, model: str | None = None,
        direct: bool = False) -> State:
    """One question, one run. No checkpointer on purpose: every question
    rediscovers the system, because the system may have changed.

    `direct=True` skips hypothesis generation: use it for questions that name
    what to look at ("does X have errors?"), keep it False for questions that
    describe a symptom ("the dashboard is frozen, why?")."""
    return build_graph(start, end, model).invoke({"question": question, "direct": direct})
