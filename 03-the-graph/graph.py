"""Step 03: the graph. A coordinator, specialists, and the code deciding when to stop.

Slides of Act III. Four questions an investigator asks (slide "Quando indago,
mi faccio quattro domande"), as LangGraph nodes over a shared state:

    discover -> meta -+-> hypotheses -> choose -> execute -> process -> weigh -+-> synthesize
                      |                  ^                                     |
                      |                  +------------- another cycle ---------+
                      +-> (a lookup: the question is the hypothesis)

What changed from step 02:

* `meta` decides whether the question is a lookup or a symptom to investigate;
* `hypotheses`: the code puts first one hypothesis per silent service
  ("store is down"), the model adds its own (slide "Un giro: un'ipotesi, un agente");
* `choose`: the code picks the hypothesis, the model picks the specialist,
  the specialist picks the tool (`common/agents.py`);
* `weigh`: the model scores, the code overrules it twice: an empty result
  discards its hypothesis, and a hypothesis never investigated keeps its score
  (slide "Una causa confermata. Le altre restano aperte.");
* `route_after_weigh`: plain `if`s decide another cycle or the answer
  (slide "Quando fermarsi lo decide il codice");
* the sentinel: "no tool of mine covers this" is a valid answer
  (slide "Un sistema che non può dire «non lo so» dirà qualcos'altro").

It closes at the first hypothesis that holds. Step 04 is what happens when
the first one that holds is the symptom.

Concepts in this file (LangGraph, in the order you meet them):

* state schema: `State`, a TypedDict with `total=False`; nodes return partial
  updates and LangGraph merges them (`State`, every `node_*`);
* nodes and edges: `add_node`, `add_edge`, `START`, `END` (`build_graph`);
* closures: the lambdas pass `start`, `end`, `model` into nodes (`build_graph`);
* conditional edges: a router returns the next node's name (`route_after_weigh`);
* `compile()` then `invoke()` (`build_graph`, `run`).
"""

from __future__ import annotations

import copy
import pathlib
import sys
import time
from typing import TypedDict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from langgraph.graph import END, START, StateGraph  # noqa: E402

from common import agents, backends, discovery, llm, tools  # noqa: E402

CONFIDENCE_THRESHOLD = 0.7
MAX_CYCLES = 3


class State(TypedDict, total=False):
    """Shared state (slide "Lo stato: la memoria condivisa"). `total=False`:
    every node returns only the fields it produced, and LangGraph merges them.

    This TypedDict is the graph's schema: each key is a channel that nodes read
    and write. A node never edits the state in place; it returns a partial dict
    ("what I changed") and LangGraph merges it in. A key with no reducer, like
    the ones here, is overwritten by the last write.
    """
    question: str
    direct: bool | None     # <- caller, or meta: True = lookup, False = symptom
    topology: dict          # <- discover
    hypotheses: list[dict]  # <- hypotheses / execute / weigh
    choice: dict            # <- choose
    findings: list[dict]    # <- execute (append-only)
    facts: str              # <- process
    silence: bool           # <- process: the last empty result is a silence that counts
    answer: str             # <- synthesize
    route: str
    error: str
    stop: bool
    cycle: int


# --------------------------------------------------------------------------
# discover, meta
# --------------------------------------------------------------------------

def node_discover(state: State, start: int, end: int) -> dict:
    """A node is a plain function: state in, partial update out.

    `start` and `end` reach it through a closure in build_graph (see there).
    """
    return {"topology": discovery.topology(start, end)}


META = {
    "type": "function",
    "function": {
        "name": "classify_question",
        "description": "Is this a lookup (it names what to look at) or a symptom to investigate?",
        "parameters": {
            "type": "object",
            "properties": {"kind": {"enum": ["lookup", "symptom"]}},
            "required": ["kind"],
        },
    },
}


def node_meta(state: State, model: str | None = None) -> dict:
    """Investigate, or just look? One call, skipped when the caller already said."""
    if state.get("direct") is not None:
        return {}   # an empty update: "I changed nothing", the state passes through
    try:
        _, args = llm.call_tool([
            {"role": "system", "content":
                "Classify the question. lookup: it names what to look at "
                "(\"does store have errors?\", \"show the logs of normalizer\"). symptom: it "
                "describes something wrong and asks why (\"data stopped arriving, what is "
                "going on?\")."},
            {"role": "user", "content": state["question"]},
        ], [META], model)
    except llm.NoToolCall:
        return {"direct": True}  # ponytail: a lookup is the cheaper wrong guess
    return {"direct": args.get("kind") != "symptom"}


# --------------------------------------------------------------------------
# hypotheses: the code's first, then the model's
# --------------------------------------------------------------------------

HYPOTHESES = {
    "type": "function",
    "function": {
        "name": "propose_hypotheses",
        "description": "Propose 2 to 4 candidate causes.",
        "parameters": {
            "type": "object",
            "properties": {
                "hypotheses": {
                    "type": "array",
                    "items": {"type": "object",
                              "properties": {"text": {"type": "string"}},
                              "required": ["text"]},
                }
            },
            "required": ["hypotheses"],
        },
    },
}


def silent_hypotheses(muted: list[dict], end: int) -> list[dict]:
    """One hypothesis per silent service, written by the code, in the order given."""
    return [{"text": f"{m['service']} is down: it wrote log lines until "
                     f"{(end - m['last_seen']) // 60} minutes ago and nothing since",
             "service": m["service"], "silent": True, "from_code": True}
            for m in muted]


def node_hypotheses(state: State, end: int, model: str | None = None) -> dict:
    if state.get("direct"):
        return {"hypotheses": [{"text": state["question"], "confidence": 0.5}]}

    topology = state["topology"]
    hypotheses = silent_hypotheses(topology.get("muted") or [], end)
    try:
        _, args = llm.call_tool([
            {"role": "system", "content":
                "You are an SRE triage agent. Propose 2-4 plausible causes for the symptom. "
                "Use the real service names you are given."},
            {"role": "user", "content":
                f"Symptom: {state['question']}\n"
                f"Services: {', '.join(topology['services'])}\n"
                f"Silent right now: {', '.join(m['service'] for m in topology.get('muted') or []) or 'none'}"},
        ], [HYPOTHESES], model)
        hypotheses += [{"text": h["text"]} for h in args.get("hypotheses") or [] if h.get("text")]
    except llm.NoToolCall as exc:
        if not hypotheses:
            return {"error": f"could not propose hypotheses: {exc}", "stop": True}
    for h in hypotheses:
        h["confidence"] = 0.5
    return {"hypotheses": hypotheses}


# --------------------------------------------------------------------------
# choose: the code picks the hypothesis, the model picks the specialist
# --------------------------------------------------------------------------

def node_choose(state: State, model: str | None = None) -> dict:
    if state.get("error"):
        return {}
    hypotheses = state.get("hypotheses") or []
    if not hypotheses:
        return {"error": "no hypothesis to investigate", "stop": True}

    # Highest confidence first, never investigated before investigated, then the
    # original order: the code's hypotheses stay ahead of the model's.
    candidates = [(i, h) for i, h in enumerate(hypotheses) if not h.get("discarded")] \
        or list(enumerate(hypotheses))
    index, target = min(candidates, key=lambda c: (-c[1].get("confidence", 0),
                                                   bool(c[1].get("investigated")), c[0]))
    try:
        _, args = llm.call_tool([
            {"role": "system", "content":
                "You coordinate an SRE team. Pick the specialist who should check this hypothesis."},
            {"role": "user", "content": target["text"]},
        ], [agents.specialist_schema()], model)
    except llm.NoToolCall as exc:
        return {"error": str(exc), "stop": True}
    return {"choice": {"hypothesis_index": index,
                       "specialist": args.get("specialist") or "logs"}}


# --------------------------------------------------------------------------
# execute + process: the specialist picks the tool, the code runs it
# --------------------------------------------------------------------------

def node_execute(state: State, start: int, end: int, model: str | None = None) -> dict:
    if state.get("error"):
        return {}
    choice = state["choice"]
    # Copy first: the state belongs to the graph. The node edits its own copy
    # and hands it back as an update.
    hypotheses = copy.deepcopy(state["hypotheses"])
    target = hypotheses[choice["hypothesis_index"]]

    hint = ""
    last = (state.get("findings") or [])[-1:]
    if last and last[0].get("hits") == 0 and not last[0].get("sentinel"):
        hint = (f"\nThe previous attempt ({last[0].get('query')}) returned nothing. "
                "Try a different angle.")
    try:
        tool, args = agents.ask_specialist(choice["specialist"], target["text"],
                                           state["topology"], model, hint)
        finding = agents.run_tool(tool, args, start, end)
    except (llm.NoToolCall, ValueError) as exc:
        return {"error": str(exc), "stop": True}

    finding["agent"] = choice["specialist"]
    target["investigated"] = True
    if finding.get("hits") or is_silence(target, finding):
        target["evidence"] = finding["query"]
    # `findings` has no reducer, so a bare [finding] would replace the history:
    # the node appends by hand. Step 04's `results` does it with a reducer.
    return {"findings": list(state.get("findings") or []) + [finding], "hypotheses": hypotheses}


def is_silence(hypothesis: dict, finding: dict) -> bool:
    """An empty result that is the evidence: ALL the logs of a service the
    hypothesis says is down, read and found empty. With a level filter, empty
    only means "no errors". Only for the code's own hypotheses about a service
    discovery saw go quiet. ponytail: silence counts, whatever its cause.

    Everywhere else an empty result is only "valid query, nothing found", which
    proves nothing; a service that stops logging is evidence only in this
    declared case.
    """
    return (bool(hypothesis.get("silent")) and finding.get("tool") == "loki_query"
            and finding.get("hits") == 0 and finding.get("ok") and not finding.get("level")
            and hypothesis.get("service") in (finding.get("services") or []))


def silence_facts(query: str, service: str, muted: list[dict], end: int) -> str:
    """What a silence says, in words the model can weigh."""
    seen = next((m["last_seen"] for m in muted if m["service"] == service), end)
    return (f"{query} returned no log lines in the observation window. {service}'s last log "
            f"line is from {time.strftime('%H:%M:%S', time.localtime(seen))}, "
            f"{(end - seen) // 60} minutes ago; it was logging before that.")


def node_process(state: State, end: int) -> dict:
    if state.get("error"):
        return {}
    last = state["findings"][-1]
    target = state["hypotheses"][state["choice"]["hypothesis_index"]]
    if last.get("sentinel"):
        return {"facts": "no tool of mine covers this question", "silence": False}
    if is_silence(target, last):
        return {"facts": silence_facts(last["query"], target["service"],
                                       state["topology"].get("muted") or [], end),
                "silence": True}
    return {"facts": backends.to_facts(last, last["tool"]), "silence": False}


# --------------------------------------------------------------------------
# weigh: the model scores, the code overrules
# --------------------------------------------------------------------------

def weigh_schema(count: int) -> dict:
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
                        "items": {"type": "object",
                                  "properties": {"index": {"type": "integer",
                                                           "description": f"0 to {max(count - 1, 0)}"},
                                                 "confidence": {"type": "number"}},
                                  "required": ["index", "confidence"]},
                    }
                },
                "required": ["scores"],
            },
        },
    }


def apply_guardrails(hypotheses: list[dict], scores: dict[int, float], index: int | None,
                     empty: bool, silence: bool) -> list[dict]:
    """The model's scores, overruled where the evidence says otherwise.

    * a hypothesis never investigated keeps its score: the model cannot confirm
      or discard what nobody looked at ("not investigated");
    * the hypothesis just investigated, if its query came back empty and that
      emptiness is not the evidence, is discarded whatever the model said. Not
      a silent-service hypothesis: a badly aimed query ("no errors") says
      nothing about a silence, so it gets another cycle instead.
    """
    out = copy.deepcopy(hypotheses)
    for i, h in enumerate(out):
        if not h.get("investigated"):
            h["not_investigated"] = True
            continue
        h.pop("not_investigated", None)
        if i in scores:
            h["confidence"] = scores[i]
    if index is not None and empty and not silence and not out[index].get("silent"):
        out[index]["confidence"] = 0
        out[index]["discarded"] = True
    return out


def node_weigh(state: State, model: str | None = None) -> dict:
    if state.get("error"):
        return {}
    hypotheses = state.get("hypotheses") or []
    listing = "\n".join(f"{i}) {h['text']}" for i, h in enumerate(hypotheses))
    try:
        _, args = llm.call_tool([
            {"role": "system", "content":
                "You are an SRE triage agent. Score each hypothesis from 0 to 1 on the observed "
                "facts. High confidence means the facts support it."},
            {"role": "user", "content":
                f"Question: {state['question']}\n\nObserved: {state.get('facts', '')}\n\n"
                f"Hypotheses:\n{listing}"},
        ], [weigh_schema(len(hypotheses))], model)
    except llm.NoToolCall as exc:
        return {"error": str(exc), "stop": True}
    scores = {s["index"]: s.get("confidence", 0) for s in args.get("scores") or []
              if isinstance(s.get("index"), int)}

    last = (state.get("findings") or [{}])[-1]
    hypotheses = apply_guardrails(hypotheses, scores, (state.get("choice") or {}).get("hypothesis_index"),
                                  empty=last.get("hits") == 0, silence=bool(state.get("silence")))
    cycle = (state.get("cycle") or 0) + 1
    all_out = all(h.get("discarded") for h in hypotheses)
    return {"hypotheses": hypotheses, "cycle": cycle, "stop": all_out and cycle >= MAX_CYCLES}


def best(state: State) -> float:
    return max((h.get("confidence", 0) for h in state.get("hypotheses") or []
                if not h.get("discarded")), default=0)


# A router: a function from the state to the NAME of the next node. It only
# reads; add_conditional_edges (see build_graph) maps each name it may return
# to a node. This is how a graph loops: "choose" sends the run back.
def route_after_weigh(state: State) -> str:
    """Another cycle, or the answer? Plain `if`s on the evidence, never the model."""
    if state.get("stop") or state.get("error") or (state.get("cycle") or 0) >= MAX_CYCLES:
        return "synthesize"
    last = (state.get("findings") or [{}])[-1]
    if last.get("sentinel"):
        undecided = [h for h in state.get("hypotheses") or [] if not h.get("discarded")]
        return "choose" if undecided and not state.get("direct") else "synthesize"
    if last.get("hits") == 0 and not state.get("silence"):
        return "choose"
    return "synthesize" if best(state) >= CONFIDENCE_THRESHOLD else "choose"


# --------------------------------------------------------------------------
# synthesize
# --------------------------------------------------------------------------

def node_synthesize(state: State, model: str | None = None) -> dict:
    if state.get("error"):
        return {"answer": f"I could not investigate: {state['error']}", "stop": True}

    findings = state.get("findings") or []
    active = [h for h in state.get("hypotheses") or [] if not h.get("discarded")]
    if findings and findings[-1].get("sentinel") and not any(h.get("evidence") for h in active):
        return {"answer": "No tool of mine covers this question: it goes beyond what this "
                          "stack exposes.", "stop": True}
    if best(state) < CONFIDENCE_THRESHOLD:
        lines = [f"- {h['text']} (confidence {h.get('confidence', 0)})" for h in active]
        tail = ("Still open:\n" + "\n".join(lines) if lines else
                "Every hypothesis was discarded: the queries that would have supported them "
                "came back empty.")
        return {"answer": f"Inconclusive: no hypothesis reached the confidence threshold "
                          f"({CONFIDENCE_THRESHOLD}). {tail}", "stop": True}

    top = max(active, key=lambda h: h.get("confidence", 0))
    role = ("Answer the question in 2-3 sentences, using only the observed facts, and cite "
            "the query the data came from. Do not invent numbers."
            if state.get("direct") else
            "State the cause in one sentence, starting with the service named in the "
            "conclusion, then cite the query it rests on. Do not invent numbers.")
    response = llm.call([
        {"role": "system", "content": "You are an SRE triage analyst. " + role},
        {"role": "user", "content":
            f"Question: {state['question']}\nConclusion: {top['text']}\n"
            f"Observed: {state.get('facts', '')}\nSource: {top.get('evidence')}"},
    ], model=model)
    return {"answer": llm.text(response), "stop": True}


# --------------------------------------------------------------------------
# wiring (slide "Nodi e archi")
# --------------------------------------------------------------------------

def build_graph(start: int, end: int, model: str | None = None):
    """Describe the graph, then compile it. Built per question.

    add_node wants a function of the state alone, so the lambdas close over
    start, end and model. They are fixed for this question and stay out of the state.
    """
    graph = StateGraph(State)
    graph.add_node("discover", lambda s: node_discover(s, start, end))
    graph.add_node("meta", lambda s: node_meta(s, model))
    graph.add_node("hypotheses", lambda s: node_hypotheses(s, end, model))
    graph.add_node("choose", lambda s: node_choose(s, model))
    graph.add_node("execute", lambda s: node_execute(s, start, end, model))
    graph.add_node("process", lambda s: node_process(s, end))
    graph.add_node("weigh", lambda s: node_weigh(s, model))
    graph.add_node("synthesize", lambda s: node_synthesize(s, model))

    # START and END are the graph's entry and exit markers. add_edge(a, b):
    # when a finishes, b runs next, always.
    graph.add_edge(START, "discover")
    graph.add_edge("discover", "meta")
    graph.add_edge("meta", "hypotheses")
    graph.add_edge("hypotheses", "choose")
    graph.add_edge("choose", "execute")
    graph.add_edge("execute", "process")
    graph.add_edge("process", "weigh")
    # A conditional edge: after "weigh" the router picks the next node by name;
    # the dict lists every name it may return.
    graph.add_conditional_edges("weigh", route_after_weigh,
                                {"choose": "choose", "synthesize": "synthesize"})
    graph.add_edge("synthesize", END)
    # Until compile() this is only a description. compile() checks the wiring
    # and returns a runnable. It executes in supersteps: all nodes that are
    # ready run, their updates are merged into the state, then the next step.
    return graph.compile()


def run(question: str, start: int, end: int, model: str | None = None,
        direct: bool | None = None) -> State:
    """One question, one run. Every question rediscovers the system: it may have changed."""
    # invoke() takes the initial state, runs START to END, and returns the final state.
    return build_graph(start, end, model).invoke({"question": question, "direct": direct})
