"""Step 04: the team behind a trigger, and the rule of who comes first.

Slides of Act IV. Step 03 closes at the first hypothesis that holds. When two
services are silent and one feeds the other, the first that holds is the
symptom: stop `normalizer` and `store` goes quiet a moment later, and step 03
says "store is down" (slide "E se il primo indizio fosse il sintomo?").

Two answers, kept together (slide "La squadra, quando la regola non basta"):

* **the rule**, zero model calls: two silent services in a chain of
  `dependencies.yaml`, the upstream one goes first (slide "Chi viene prima,
  scritto nel codice"). `order_by_rule` does it in discover;
* **the team**, only behind a trigger: a chain among the silent services, or
  two hypotheses above the threshold at once. One agent per
  hypothesis, all at once with `Send`; their results pile up through a
  reducer; `collect` compares them and decides where to go with a `Command`
  (slides "Un agente per ipotesi, tutti insieme", "Un nodo che decide anche
  dove andare"). Evidence that cannot tell two hypotheses apart is settled by
  the rule (slide "La squadra confronta, ma le prove sono uguali").

    discover -> meta -> hypotheses -> choose -> execute -> process -> weigh -+-> synthesize
                                        ^                                    |
                                        +---- no trigger: as in step 03 -----+
                                                                             |
                                                         trigger -> team ----+

Diff this folder against 03-the-graph to see exactly what Act IV adds.
"""

from __future__ import annotations

import copy
import operator
import pathlib
import sys
import time
from typing import Annotated, TypedDict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command, Send  # noqa: E402

from common import agents, backends, discovery, llm, tools  # noqa: E402

CONFIDENCE_THRESHOLD = 0.7
MAX_CYCLES = 3
TEAM_SIZE = 3       # agents sent at once: the top hypotheses
DEPENDENCIES = pathlib.Path(__file__).resolve().parent / "dependencies.yaml"


class State(TypedDict, total=False):
    """Shared state (slide "Lo stato: la memoria condivisa"). `total=False`:
    every node returns only the fields it produced, and LangGraph merges them."""
    question: str
    direct: bool | None     # <- caller, or meta: True = lookup, False = symptom
    topology: dict          # <- discover
    hypotheses: list[dict]  # <- hypotheses / execute / weigh
    choice: dict            # <- choose
    findings: list[dict]    # <- execute (append-only)
    facts: str              # <- process
    silence: bool           # <- process: the last empty result is a silence that counts
    answer: str             # <- synthesize
    route: str              # <- team: why the team ran, and what it decided
    chosen: int             # <- team: the hypothesis it settled on
    team_done: bool         # <- team: it runs at most once per question
    error: str
    stop: bool
    cycle: int


# --------------------------------------------------------------------------
# discover, meta
# --------------------------------------------------------------------------

def chains(muted: list[dict], dependencies: list[dict]) -> list[dict]:
    """Edges of `dependencies.yaml` whose two ends are both silent right now."""
    silent = {m["service"] for m in muted}
    return [d for d in dependencies if d["from"] in silent and d["to"] in silent]


def order_by_rule(muted: list[dict], dependencies: list[dict]) -> list[dict]:
    """The rule: of two silent services in a chain, the upstream one goes first.

    A stable sort: services in a chain first, and among them by "how many
    silent services are upstream of me", so a service fed by nobody silent
    comes before its consumers. Services outside any chain follow, in the order
    discovery gave them.
    """
    edges = chains(muted, dependencies)
    linked = {e["from"] for e in edges} | {e["to"] for e in edges}

    def upstream(service: str, seen: frozenset = frozenset()) -> int:
        parents = [e["from"] for e in edges if e["to"] == service and e["from"] not in seen]
        return max((1 + upstream(p, seen | {service}) for p in parents), default=0)

    return sorted(muted, key=lambda m: (m["service"] not in linked, upstream(m["service"])))


def node_discover(state: State, start: int, end: int) -> dict:
    topology = discovery.topology(start, end)
    dependencies = yaml.safe_load(DEPENDENCIES.read_text()) or []
    topology["dependencies"] = dependencies
    topology["chains"] = chains(topology["muted"], dependencies)
    topology["muted"] = order_by_rule(topology["muted"], dependencies)
    return {"topology": topology}


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
        return {}
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
    return {"findings": list(state.get("findings") or []) + [finding], "hypotheses": hypotheses}


def is_silence(hypothesis: dict, finding: dict) -> bool:
    """An empty result that is the evidence: ALL the logs of a service the
    hypothesis says is down, read and found empty. With a level filter, empty
    only means "no errors". Only for the code's own hypotheses about a service
    discovery saw go quiet. ponytail: silence counts, whatever its cause."""
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


def trigger(state: State) -> str:
    """Why the team should run, or "" (slide "La squadra, quando la regola non basta").

    Decided by the code, after every weigh: a chain among the silent services
    (true from the first cycle), or two investigated hypotheses above the
    threshold at once (possible from the second). The team runs at most once.
    """
    edges = state["topology"].get("chains") or []
    if edges:
        return "chain " + ", ".join(f"{e['from']} -> {e['to']}" for e in edges)
    strong = [h for h in state.get("hypotheses") or []
              if not h.get("discarded") and h.get("confidence", 0) >= CONFIDENCE_THRESHOLD]
    return "two hypotheses above the threshold" if len(strong) >= 2 else ""


def route_after_weigh(state: State) -> str:
    """Another cycle, the answer, or the team? Plain `if`s on the evidence, never the model."""
    if state.get("stop") or state.get("error"):
        return "synthesize"
    if not state.get("direct") and not state.get("team_done") and trigger(state):
        return "team"
    if (state.get("cycle") or 0) >= MAX_CYCLES:
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

    chosen = state.get("chosen")
    top = (state["hypotheses"][chosen] if chosen is not None
           else max(active, key=lambda h: h.get("confidence", 0)))
    role = ("Answer the question in 2-3 sentences, using only the observed facts, and cite "
            "the query the data came from. Do not invent numbers."
            if state.get("direct") else
            "State the cause in one sentence, starting with the service named in the "
            "conclusion, then cite the query it rests on. Do not invent numbers.")
    response = llm.call([
        {"role": "system", "content": "You are an SRE triage analyst. " + role},
        {"role": "user", "content":
            f"Question: {state['question']}\nConclusion: {top['text']}\n"
            f"Observed: {top.get('facts') or state.get('facts', '')}\nSource: {top.get('evidence')}"},
    ], model=model)
    return {"answer": llm.text(response), "stop": True}


# --------------------------------------------------------------------------
# the team: a subgraph, one agent per hypothesis, all at once
# --------------------------------------------------------------------------

class TeamState(TypedDict, total=False):
    question: str
    topology: dict
    hypotheses: list[dict]
    # The reducer: every agent appends its result, none overwrites another's.
    results: Annotated[list[dict], operator.add]
    round: int
    verdict: dict


def node_spawn(state: TeamState) -> dict:
    return {"round": (state.get("round") or 0) + 1}


def send_agents(state: TeamState) -> list[Send]:
    """One `Send` per hypothesis: they start together, each with its own private state."""
    hypotheses = state["hypotheses"]
    open_ = [i for i, h in enumerate(hypotheses) if not h.get("discarded")]
    top = sorted(open_, key=lambda i: -hypotheses[i].get("confidence", 0))[:TEAM_SIZE]
    before = {r["index"]: r.get("facts") for r in state.get("results") or []
              if r["round"] == state["round"] - 1}
    return [Send("investigate", {"question": state["question"], "topology": state["topology"],
                                 "hypothesis": hypotheses[i], "index": i, "round": state["round"],
                                 "before": before.get(i)})
            for i in top]


SCORE_ONE = {
    "type": "function",
    "function": {
        "name": "score_hypothesis",
        "description": "How far do the observed facts support the hypothesis? 0 to 1.",
        "parameters": {"type": "object", "properties": {"confidence": {"type": "number"}},
                       "required": ["confidence"]},
    },
}


def node_investigate(task: dict, start: int, end: int, model: str | None = None) -> dict:
    """One agent, one hypothesis: specialist, tool, facts, score. Three calls.

    The same guardrail as the coordinator's weigh: an empty result that is not
    the evidence leaves the hypothesis at 0, whatever the model said.
    """
    h = task["hypothesis"]
    result = {"index": task["index"], "round": task["round"], "confidence": 0, "finding": None}
    try:
        _, args = llm.call_tool([
            {"role": "system", "content":
                "You coordinate an SRE team. Pick the specialist who should check this hypothesis."},
            {"role": "user", "content": h["text"]},
        ], [agents.specialist_schema()], model)
        specialist = args.get("specialist") or "logs"
        hint = (f"\nThe previous round found: {task['before']} It did not settle the "
                "hypothesis: look from a different angle." if task.get("before") else "")
        tool, tool_args = agents.ask_specialist(specialist, h["text"], task["topology"], model, hint)
        finding = agents.run_tool(tool, tool_args, start, end)
    except (llm.NoToolCall, ValueError) as exc:
        return {"results": [{**result, "error": str(exc)}]}

    finding["agent"] = f"team/{specialist}"
    silence = is_silence(h, finding)
    if finding.get("sentinel"):
        facts = "no tool of mine covers this question"
    elif silence:
        facts = silence_facts(finding["query"], h["service"], task["topology"].get("muted") or [], end)
    else:
        facts = backends.to_facts(finding, finding["tool"])
    result.update(finding=finding, facts=facts, silence=silence)
    if not finding.get("hits") and not silence:
        return {"results": [result]}
    try:
        _, args = llm.call_tool([
            {"role": "system", "content": "You are an SRE triage agent. Score the hypothesis on "
                                          "the observed facts only."},
            {"role": "user", "content": f"Question: {task['question']}\nHypothesis: {h['text']}\n"
                                        f"Observed: {facts}"},
        ], [SCORE_ONE], model)
    except llm.NoToolCall as exc:
        return {"results": [{**result, "error": str(exc)}]}
    result.update(confidence=args.get("confidence", 0), evidence=finding["query"])
    return {"results": [result]}


def upstream_rank(hypothesis: dict, topology: dict) -> int:
    """Position of the hypothesis's service in the rule's order (unknown = last)."""
    order = [m["service"] for m in topology.get("muted") or []]
    service = hypothesis.get("service")
    return order.index(service) if service in order else len(order)


def node_collect(state: TeamState) -> Command:
    """Compare the results, then decide where to go: the answer, or one replica."""
    mine = [r for r in state.get("results") or [] if r["round"] == state["round"]]
    top = max((r["confidence"] for r in mine), default=0)
    if top < CONFIDENCE_THRESHOLD and state["round"] < 2:
        return Command(goto="spawn")   # nobody convinced: one more round, then stop

    held = [r for r in mine if r["confidence"] >= CONFIDENCE_THRESHOLD and r["confidence"] >= top - 0.05]
    hypotheses = state["hypotheses"]
    if len(held) > 1:
        # Same evidence for both: the comparison cannot separate them. The rule can.
        held.sort(key=lambda r: upstream_rank(hypotheses[r["index"]], state["topology"]))
        note = (f"{len(held)} hypotheses held on equal evidence; the rule picked the one upstream")
    else:
        note = "one hypothesis held" if held else "no hypothesis held"
    chosen = held[0]["index"] if held else None
    return Command(goto=END, update={"verdict": {"chosen": chosen, "note": note}})


def build_team(start: int, end: int, model: str | None = None):
    # Compiled per question, like build_graph: start, end and model are closed over.
    team = StateGraph(TeamState)
    team.add_node("spawn", node_spawn)
    team.add_node("investigate", lambda t: node_investigate(t, start, end, model))
    team.add_node("collect", node_collect, destinations=("spawn", END))
    team.add_edge(START, "spawn")
    team.add_conditional_edges("spawn", send_agents, ["investigate"])
    team.add_edge("investigate", "collect")
    return team.compile()


def node_team(state: State, start: int, end: int, model: str | None = None) -> dict:
    """The subgraph, run as one node of the coordinator's graph."""
    out = build_team(start, end, model).invoke(
        {"question": state["question"], "topology": state["topology"],
         "hypotheses": state["hypotheses"]})
    hypotheses = copy.deepcopy(state["hypotheses"])
    last_round = [r for r in out.get("results") or [] if r["round"] == out.get("round")]
    findings = list(state.get("findings") or [])
    for r in last_round:
        if r.get("error"):
            continue   # an agent that failed did not look: the hypothesis stays as it was
        h = hypotheses[r["index"]]
        h.update(investigated=True, confidence=r["confidence"], facts=r.get("facts"))
        h.pop("not_investigated", None)
        if r.get("evidence"):
            h["evidence"] = r["evidence"]
        finding = r.get("finding") or {}
        if finding:
            findings.append(finding)
            if not finding.get("hits") and not r.get("silence") and not h.get("silent"):
                h.update(confidence=0, discarded=True)   # the coordinator's guardrail, here too
    verdict = out.get("verdict") or {}
    if verdict.get("chosen") is None:
        for h in hypotheses:
            h["confidence"] = min(h.get("confidence", 0), CONFIDENCE_THRESHOLD - 0.01)
    return {"hypotheses": hypotheses, "findings": findings, "chosen": verdict.get("chosen"),
            "team_done": True,
            "route": f"team (trigger: {trigger(state)}; {out.get('round')} round(s); "
                     f"{verdict.get('note', '')})"}

# --------------------------------------------------------------------------
# wiring (slide "Nodi e archi")
# --------------------------------------------------------------------------

def build_graph(start: int, end: int, model: str | None = None):
    graph = StateGraph(State)
    graph.add_node("discover", lambda s: node_discover(s, start, end))
    graph.add_node("meta", lambda s: node_meta(s, model))
    graph.add_node("hypotheses", lambda s: node_hypotheses(s, end, model))
    graph.add_node("choose", lambda s: node_choose(s, model))
    graph.add_node("execute", lambda s: node_execute(s, start, end, model))
    graph.add_node("process", lambda s: node_process(s, end))
    graph.add_node("weigh", lambda s: node_weigh(s, model))
    graph.add_node("synthesize", lambda s: node_synthesize(s, model))
    graph.add_node("team", lambda s: node_team(s, start, end, model))

    graph.add_edge(START, "discover")
    graph.add_edge("discover", "meta")
    graph.add_edge("meta", "hypotheses")
    graph.add_edge("hypotheses", "choose")
    graph.add_edge("choose", "execute")
    graph.add_edge("execute", "process")
    graph.add_edge("process", "weigh")
    graph.add_conditional_edges("weigh", route_after_weigh,
                                {"choose": "choose", "synthesize": "synthesize", "team": "team"})
    graph.add_edge("team", "synthesize")
    graph.add_edge("synthesize", END)
    return graph.compile()


def run(question: str, start: int, end: int, model: str | None = None,
        direct: bool | None = None) -> State:
    """One question, one run. Every question rediscovers the system: it may have changed."""
    return build_graph(start, end, model).invoke({"question": question, "direct": direct})
