# Observability agent lab

Ask a model for a LogQL query about your `store` service and it writes
`{service="store"}`. The query is valid, the label is `service_name`, nothing
comes back, and the model reports that store "did not produce any log lines".
An empty answer looks like an answer.

> **Don't let the model write the query. Let it pick from queries you know are
> valid, and let the code decide when to stop.**

Companion code of the talk **"L'incidente non parla PromQL"** (DevFest Milano
2026): [talk page](https://montelli.dev/talks/incidente-non-parla-promql/) ·
[slides (PDF, in Italian)](slides/talk-promql-devfest-milano-2026.pdf).

## Four steps, one per act

| Folder | What it adds |
|---|---|
| [`01-the-model-writes`](01-the-model-writes/main.py) | the model writes LogQL/PromQL |
| [`02-the-model-picks`](02-the-model-picks/main.py) | the model picks from `enum`s built at runtime, the code writes the query |
| [`03-the-graph`](03-the-graph/graph.py) | a LangGraph loop: hypotheses, specialists, guardrails, a router, "I don't know" |
| [`04-the-team`](04-the-team/graph.py) | the rule "upstream first", a trigger, a team of agents (`Send`, reducer, `Command`) |

Each step runs on its own; `diff` two folders to see what an act adds. Every
file opens with the LangGraph and Loki/Mimir concepts it uses.

Measured with `mistral/codestral-2508` (bench on 2026-10-09, failures on
2026-10-10, three questions times three runs):

| | 01 | 02 | 03 | 04 |
|---|---|---|---|---|
| bench, healthy system (L3) | 0/5 | 3/5 | 4/5 | 4/5 |
| `store` stopped | | | 9/9 | 9/9 |
| `normalizer` stopped | | | 0/9 | 8/9 |

With normalizer stopped, store goes quiet too: step 03 names the symptom,
step 04 the cause, at 13 model calls per investigation instead of 5.

## Quick start

You need Docker with Compose v2, Python 3.10-3.14 and a model (hosted with a
key, or local). Everything else, including the model routes and what to do
when something fails, is in [SETUP.md](SETUP.md).

```bash
git clone --recurse-submodules https://github.com/monte97/observability-agent-lab
cd observability-agent-lab
make setup                # venv, dependencies, .env from .env.example
$EDITOR .env              # AGENT_MODEL and its key
make up                   # then give the services two minutes to log
make check                # backends 200, venv ok, model key ok
make ask STEP=02 Q="which log lines did the store service produce recently?"
```

To see the talk's point live, two failures and steps 03 and 04 side by side:
[Run the demo end to end](SETUP.md#run-the-demo-end-to-end).

Working on the code, by hand or with a coding agent: [AGENTS.md](AGENTS.md)
has the layout, the rules that must hold, and how to verify a change.

## What is not here

Step 04 rewrites the mechanisms of the system shown on stage, kept small. Not
its rules: a silent service always counts as stopped here (the stage system
tells stopped from idle), and `dependencies.yaml` is written by hand (the stage
system reads the documentation).

MIT license.
