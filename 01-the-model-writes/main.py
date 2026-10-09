"""Step 01: the model writes the query.

The first thing everyone tries (slide "La cosa che si prova per prima"): the
question in plain language, the model writes LogQL or PromQL, the backend runs
it. Two calls: one to write the query, one to answer from what came back.

    make ask STEP=01 Q="which log lines did the store service produce recently?"
    make ask STEP=01 WITH_NAMES=1 Q="..."     # the real names go in the prompt

Slide "Dati veri, domanda sbagliata": with the real names in the prompt the
queries stop coming back empty, and the answers stay wrong. Run the bench on
this step to see it here: `make bench STEP=01`.
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import backends, discovery, llm  # noqa: E402

WRITE_QUERY = {
    "type": "function",
    "function": {
        "name": "write_query",
        "description": "Write one query that answers the question.",
        "parameters": {
            "type": "object",
            "properties": {
                "backend": {"enum": ["loki", "mimir"],
                            "description": "loki for logs (LogQL), mimir for metrics (PromQL)."},
                "query": {"type": "string", "description": "The LogQL or PromQL query."},
            },
            "required": ["backend", "query"],
        },
    },
}


def names_in_prompt(topology: dict) -> str:
    return (f"\nServices: {', '.join(topology['services'])}\n"
            f"Metrics: {', '.join(topology['metrics'][:60])}"
            f"{' ...' if len(topology['metrics']) > 60 else ''}\n"
            "Log label for the service: service_name.")


def run(question: str, start: int, end: int, model: str | None = None, direct=None) -> dict:
    system = ("You are an SRE assistant. Write one query that answers the question: LogQL "
              "for Loki if it is about logs, PromQL for Mimir if it is about metrics.")
    if os.environ.get("WITH_NAMES"):
        system += names_in_prompt(discovery.topology(start, end))

    _, args = llm.call_tool([{"role": "system", "content": system},
                             {"role": "user", "content": question}], [WRITE_QUERY], model)
    query = args.get("query") or ""
    if args.get("backend") == "mimir":
        finding = {"tool": "mimir_query", **backends.run_mimir(query, start, end)}
    else:
        finding = {"tool": "loki_query", **backends.run_loki(query, start, end)}

    response = llm.call([
        {"role": "system", "content": "You are an SRE assistant. Answer in 2-3 sentences, "
                                      "using only the observed facts, and cite the query."},
        {"role": "user", "content": f"Question: {question}\n"
                                    f"Observed: {backends.to_facts(finding, finding['tool'])}"},
    ], model=model)
    return {"findings": [finding], "answer": llm.text(response)}


if __name__ == "__main__":
    from common.cli import main
    sys.exit(main(run, __doc__))
