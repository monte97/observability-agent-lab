"""Step 02: the model picks, the code writes the query.

Slide "Dallo scrivere allo scegliere": the model no longer writes LogQL or
PromQL. It picks a tool and fills its parameters from an `enum` built a moment
ago from what the backends contain (`common/discovery.py`). A service that does
not exist is not unlikely: it is unsayable. The query string is composed by
code the model never sees (`common/tools.py`).

One agent, every tool, one choice, one answer: two calls. What it still lacks,
on purpose, is the subject of step 03:

* no way to say "none of my tools answers this", so it answers anyway
  (try "how many users abandoned their shopping cart today?");
* no loop: one look, and whatever came back is the answer;
* every tool in the same call (slide "Perché non un agente solo?").

    make ask STEP=02 Q="which log lines did the store service produce recently?"
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common import agents, backends, discovery, llm, tools  # noqa: E402


def run(question: str, start: int, end: int, model: str | None = None, direct=None) -> dict:
    topology = discovery.topology(start, end)
    name, args = llm.call_tool(
        [
            {"role": "system", "content":
                "You are an SRE assistant. Pick exactly one tool to answer the question."},
            {"role": "user", "content": question},
        ],
        [tools.loki_schema(topology["services"]),
         tools.mimir_schema(topology["metrics"], topology["labels"])],
        model,
    )
    finding = agents.run_tool(name, args, start, end)

    response = llm.call([
        {"role": "system", "content": "You are an SRE assistant. Answer in 2-3 sentences, "
                                      "using only the observed facts, and cite the query."},
        {"role": "user", "content": f"Question: {question}\n"
                                    f"Observed: {backends.to_facts(finding, name)}"},
    ], model=model)
    return {"findings": [finding], "answer": llm.text(response)}


if __name__ == "__main__":
    from common.cli import main
    sys.exit(main(run, __doc__))
