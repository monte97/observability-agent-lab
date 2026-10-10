"""Specialists: a role, its tools, its vocabulary.

The slide "Un ruolo, i suoi strumenti, il suo vocabolario" in code. A
specialist is one model call: given a hypothesis, pick one of *its* tools and
fill the parameters from *its* vocabulary. The query is then composed and run
by `run_tool`, which never involves the model.

Step 02 hands every tool to a single agent. From step 03 on, a coordinator
first picks the specialist, then the specialist picks the tool: fewer options
per call, one prompt per role. One exception: when the hypothesis is the code's
own "X is down", the code writes the check itself (see `node_execute`).
"""

from __future__ import annotations

from common import backends, llm, tools

SPECIALISTS = {
    "logs": "Application logs in Loki: what a service wrote, filtered by level. "
            "Also the place to check whether a service is writing anything at all.",
    "metrics": "Metrics in Mimir: counters, gauges and rates (JVM, HTTP, Kafka clients).",
}


def specialist_tools(name: str, topology: dict) -> list[dict]:
    """The tools of one specialist, with enums filled from the topology."""
    if name == "logs":
        own = tools.loki_schema(topology["services"])
    else:
        own = tools.mimir_schema(topology["metrics"], topology["labels"])
    return [own, tools.sentinel_schema()]


def specialist_schema() -> dict:
    """The coordinator's menu: which specialist should look at this?"""
    return {
        "type": "function",
        "function": {
            "name": "pick_specialist",
            "description": "Choose the specialist best placed to check the hypothesis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "specialist": {
                        "enum": list(SPECIALISTS),
                        "description": " | ".join(f"{k}: {v}" for k, v in SPECIALISTS.items()),
                    },
                },
                "required": ["specialist"],
            },
        },
    }


def ask_specialist(name: str, hypothesis: str, topology: dict, model: str | None = None,
                   hint: str = "") -> tuple[str, dict]:
    """One call: the specialist picks a tool and its parameters, or the sentinel."""
    return llm.call_tool(
        [
            {"role": "system", "content":
                f"You are the {name} specialist of an SRE team. {SPECIALISTS[name]} "
                "Pick exactly one tool to check the hypothesis. If none of your tools can "
                "check it, pick the sentinel tool." + hint},
            {"role": "user", "content": hypothesis},
        ],
        specialist_tools(name, topology),
        model,
    )


def run_tool(tool: str, args: dict, start: int, end: int) -> dict:
    """Compose the query from the chosen parameters and run it. No model here."""
    if tool == tools.SENTINEL:
        return {"tool": tool, "ok": True, "hits": 0, "sample": [], "query": None, "sentinel": True}
    if tool == "loki_query":
        query = tools.loki_compose(args.get("services") or [], args.get("level"))
        return {"tool": tool, "services": args.get("services") or [], "level": args.get("level"),
                **backends.run_loki(query, start, end)}
    if tool == "mimir_query":
        query = tools.mimir_compose(args.get("metric"), args.get("label_filters"),
                                    args.get("rate_window"))
        return {"tool": tool, **backends.run_mimir(query, start, end)}
    raise ValueError(f"unknown tool: {tool}")
