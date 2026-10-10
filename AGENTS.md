# AGENTS.md

Guide for coding agents (and people) working on this repo. Setup is in
[SETUP.md](SETUP.md); what the repo is for is in [README.md](README.md).

## What this is

A teaching repo for the talk "L'incidente non parla PromQL": an LLM agent that
answers questions about a running system by querying Loki (logs) and Mimir
(metrics), built with LangGraph and LiteLLM. It grows in four steps, one folder
each, and every step runs on its own:

| Folder | What it adds | Entry point |
|---|---|---|
| `01-the-model-writes/` | the model writes LogQL/PromQL | `main.py` |
| `02-the-model-picks/` | the model picks tool parameters from runtime `enum`s, the code writes the query | `main.py` |
| `03-the-graph/` | the LangGraph loop: meta, hypotheses, coordinator, specialists, weigh, router, sentinel | `graph.py` |
| `04-the-team/` | 03 plus `dependencies.yaml`, the upstream-first rule, the trigger, the team subgraph (`Send`, reducer, `Command`) | `graph.py` |
| `common/` | discovery, backends, tool schemas and composers, specialists, model client, CLI | |
| `bench/` | `run.py` (L1/L2/L3 per step), `incident.py` (steps compared after a failure) | |

The observed system is the `stack/` submodule (iot-observability-demo):
`device-gateway -[telemetry.raw]-> normalizer -[telemetry.clean]-> store -> MongoDB`.

## Rules that must hold

1. **From step 02 on, the model never writes a query.** It fills the parameters
   of a tool whose allowed values are an `enum` filled by `common/discovery.py`;
   `loki_compose` / `mimir_compose` in `common/tools.py` write the string. A new
   tool needs all three: a `*_schema` with runtime enums, a `*_compose`, and a
   branch in `common/agents.py:run_tool`.
2. **Discovery and backends never call the model.** `common/discovery.py` and
   `common/backends.py` are plain HTTP.
3. **Every model call goes through `common/llm.py`** (`call`, `call_tool`).
   `llm.CALLS` counts them, and the bench reports calls per investigation.
4. **The guardrails are code.** In `apply_guardrails`: an empty result discards
   its hypothesis, a hypothesis nobody investigated keeps its score. The router
   (`route_after_weigh`) and the team's trigger are plain `if`s on the state.
   Silence counts as evidence only where `is_silence` says so.
5. **`04-the-team/graph.py` is a copy of `03-the-graph/graph.py` plus Act IV.**
   A change to a function they share goes into both files, identical, comments
   included. `diff 03-the-graph/graph.py 04-the-team/graph.py` must show only
   what Act IV adds.
6. **Every step exposes the same `run`**:
   `run(question, start, end, model=None, direct=None) -> dict` with `findings`
   and `answer` (and `hypotheses` from step 03 on). `bench/steps.py` loads a step
   by its number and relies on it.
7. **Numbers in the docs are measured.** Every figure in README.md comes from a
   run, with its date and model. Re-measure before changing one.

## Run it and read the output

```bash
make check                                    # backends, venv, model key
make ask STEP=03 Q="does store have errors?"  # a lookup
make scenario STEP=04 MINUTES=2 Q="Data no longer reaches MongoDB. What is going on?"
make stack-state                              # services, silent ones, metrics
```

The output lists each step (`agent: tool -> N results   query`), the
hypotheses with their confidence, the route the team took if it ran, the
answer, and the model calls. Flags on a hypothesis:

- `[from the code]`: written by `silent_hypotheses`, one per silent service;
- `[not investigated]`: nobody looked at it, so the model could not move it;
- `[discarded]`: its query came back empty and the emptiness was not evidence.

`make ask` lets the `meta` node classify the question as a lookup or a symptom;
`make scenario` passes `--scenario` and `make examples` passes `--direct`, and
both skip `meta`.

## Verify a change

| You changed | Run |
|---|---|
| anything | `make test` (no model, no stack) and `.venv/bin/python -m py_compile` on the files |
| a step's prompts or logic | `make ask STEP=NN` on a lookup, then `make bench STEP=NN` |
| steps 03 or 04 | also `make incident` / `make incident-upstream`, wait 150 s, `make compare SERVICE=...` |
| comments only | the AST of the file, docstrings stripped, must be unchanged |

`make test` runs `04-the-team/test_rules.py` (rule, trigger, silence, guardrails,
team tie-break) and the self-check of `common/llm.py`.

## Where to change what

- **A specialist**: `SPECIALISTS` and `specialist_tools` in `common/agents.py`.
- **A dependency between services**: `04-the-team/dependencies.yaml`.
- **Thresholds**: `CONFIDENCE_THRESHOLD`, `MAX_CYCLES`, `TEAM_SIZE` at the top of
  the step's `graph.py`; `HISTORY` (how far back "it used to log" looks) in
  `common/discovery.py`.
- **A bench question**: `bench/questions.yaml`, with a gold query. Keep the one
  unanswerable question.

## Conventions

- Code, comments and docs in English; slide titles stay in Italian.
- Comments explain why. Each file opens with the concepts it uses (LangGraph,
  LogQL, PromQL, the Loki and Mimir APIs) and explains them where they appear.
- A deliberate simplification is marked `ponytail:` and names its limit.
- No em-dashes in prose; no padding words ("simply", "just", "obviously").
- Never commit `.env` or a key. `results_*.csv` are gitignored.

## Out of scope

The system shown on stage is private. Its rules are not to be reconstructed
here: telling a stopped service from an idle one, deriving dependencies from
the documentation, the measured thresholds.
