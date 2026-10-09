# Observability agent lab

Ask a model for a LogQL query about your `store` service and it writes
`{service="store"}`. The query is valid, the label is `service_name`, nothing
comes back, and the model reports that store "did not produce any log lines".
An empty answer looks like an answer.

> **Don't let the model write the query. Let it pick from queries you know are
> valid, and let the code decide when to stop.**

Companion code of the talk **"L'incidente non parla PromQL"** (DevFest Milano
2026): [talk page](https://montelli.dev/talks/incidente-non-parla-promql/) ·
[slides (PDF)](https://montelli.dev/files/talk-promql-devfest-milano-2026.pdf).

## Four steps, one per act

| Folder | What it adds |
|---|---|
| [`01-the-model-writes`](01-the-model-writes/main.py) | the model writes LogQL/PromQL |
| [`02-the-model-picks`](02-the-model-picks/main.py) | the model picks from `enum`s built at runtime, the code writes the query |
| [`03-the-graph`](03-the-graph/graph.py) | a LangGraph loop: hypotheses, specialists, guardrails, a router, "I don't know" |
| [`04-the-team`](04-the-team/graph.py) | the rule "upstream first", a trigger, a team of agents (`Send`, reducer, `Command`) |

Each step runs on its own; `diff` two folders to see what an act adds. Every
file opens with the LangGraph and Loki/Mimir concepts it uses.

Measured on 2026-10-09 with `mistral/codestral-2508`:

| | 01 | 02 | 03 | 04 |
|---|---|---|---|---|
| bench, healthy system (L3) | 0/5 | 3/5 | 4/5 | 4/5 |
| `store` stopped | | | 9/9 | 9/9 |
| `normalizer` stopped | | | 1/9 | 9/9 |

With normalizer stopped, store goes quiet too: step 03 names the symptom,
step 04 the cause, at about 13 model calls per investigation.

## Quick start

```bash
git clone --recurse-submodules https://github.com/monte97/observability-agent-lab
cd observability-agent-lab
make setup && make up && make check     # then put your model key in .env
make ask STEP=02 Q="which log lines did the store service produce recently?"
make incident-upstream && sleep 150     # stop normalizer
make compare SERVICE=normalizer         # steps 03 and 04 side by side
make healthy
```

`make help` lists the rest. The observed system is
[iot-observability-demo](https://github.com/monte97/iot-observability-demo), in
`stack/`. On macOS `make up` skips `node-exporter`, which Docker Desktop refuses.

## Pick your model

Any model [LiteLLM](https://docs.litellm.ai/docs/providers) reaches, as long as
it supports required tool calls. In `.env`:

- a hosted provider: `AGENT_MODEL=provider/model` plus its key
  (`mistral/codestral-2508` + `MISTRAL_API_KEY`);
- a gateway or local server: the bare model name plus `LLM_BASE_URL`.

Verified from a clean clone: `mistral/codestral-2508` and `qwen3.5-4b-mlx` in
LM Studio with no key, both 4/5 on step 03.

## What is not here

Step 04 rewrites the mechanisms of the system shown on stage, kept small. Not
its rules: a silent service always counts as stopped here (the stage system
tells stopped from idle), and `dependencies.yaml` is written by hand (the stage
system reads the documentation).

MIT license.
