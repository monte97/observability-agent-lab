# Setup

From a clean clone to the first answer. The first `make up` pulls and builds
the container images, which is the slow part.

## What you need

- **Docker** with Compose v2 (`docker compose version`).
- **Python 3.10 to 3.14**: LangGraph and LiteLLM require it.
- **A model** that supports required tool calls: a hosted provider with its
  key, or a local server (LM Studio, Ollama, vLLM). See [Pick your model](#pick-your-model).
- **Free host ports**: 3000 (Grafana), 3100 (Loki), 3200 (Tempo), 9009 (Mimir),
  9093 (Alertmanager), 8090 (frontend), 18080 (device-gateway), 13133, 14317,
  14318 and 15679 (OpenTelemetry Collector).

## Install

```bash
git clone --recurse-submodules https://github.com/monte97/observability-agent-lab
cd observability-agent-lab
make setup        # .venv, dependencies, and .env copied from .env.example
```

Cloned without `--recurse-submodules`? `stack/` is empty and `make up` fails:

```bash
git submodule update --init --recursive
```

## Pick your model

Edit `.env`. Every call goes through [LiteLLM](https://docs.litellm.ai/docs/providers),
which offers two routes:

| You want | `AGENT_MODEL` | Also set |
|---|---|---|
| A hosted provider | `provider/model`, e.g. `mistral/codestral-2508` | that provider's own key: `MISTRAL_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` |
| A gateway or a local server | the model name as the endpoint lists it, no slash | `LLM_BASE_URL` (LM Studio: `http://localhost:1234/v1`), plus `LLM_API_KEY` if the endpoint wants one |

A `provider/model` name never goes to `LLM_BASE_URL`, and a bare name never
reads a provider's key. Mixing the two is the usual reason for a request that
fails with an unknown model.

The model must support `tool_choice="required"`: the agent never accepts prose
where a tool call is expected. Verified on 2026-10-09: `mistral/codestral-2508`,
and `qwen3.5-4b-mlx` in LM Studio with no key (slower, same bench score).

`.env` is in `.gitignore`. Keep your keys there and nowhere else.

## Start and check

```bash
make up           # ~13 containers; the first time it pulls and builds the images
make check        # the three backends answer 200, venv ok, model key ok
```

Give the services two minutes after `make up`: the load generator has to
produce traffic before there are logs to find. Then:

```bash
make stack-state                      # services: [device-gateway, normalizer, store]
make ask STEP=02 Q="which log lines did the store service produce recently?"
```

## Run the demo end to end

The talk's point in two failures. The observation window is short
(`MINUTES=2`), so a stopped service falls silent within it after 150 seconds.

**1. The consumer stops.** The first hypothesis that holds is the cause:

```bash
make incident && sleep 150            # stop store
make scenario STEP=03 MINUTES=2 Q="Data no longer reaches MongoDB. What is going on?"
make healthy
```

Expect `store is down: it wrote log lines until 2 minutes ago and nothing
since`, confirmed by `{service_name=~"store"}` returning nothing, in about 5
model calls.

**2. The producer stops, and the consumer goes quiet too.** Now the first
hypothesis that holds is the symptom:

```bash
make incident-upstream && sleep 150   # stop normalizer
make scenario STEP=03 MINUTES=2 Q="Data no longer reaches MongoDB. What is going on?"
make scenario STEP=04 MINUTES=2 Q="Data no longer reaches MongoDB. What is going on?"
make healthy
```

Expect step 03 to answer `store is down` (the symptom) and step 04 to answer
`normalizer is down` (the cause), with `route: team (trigger: chain normalizer
-> store ...)` and about 13 model calls. `make compare SERVICE=normalizer`
runs both steps on three questions, three times each, and counts.

The model is not deterministic: measured on 2026-10-10, step 04 named the
cause 8 times out of 9, step 03 never.

`make down` stops the stack when you are done.

## When something fails

| Symptom | Cause | Fix |
|---|---|---|
| `make check` says `model key: MISSING` | `.env` has no key and no `LLM_BASE_URL` | set one of the two routes above |
| `The model call failed: ... 400` mentioning `tool_choice` | the model cannot do required tool calls | pick another model |
| `The model call failed: ... 503` or `high demand` | the provider is overloaded | retry, or switch model |
| every answer says the query returned nothing | the services have not logged yet, or the stack is not up | wait two minutes; `make stack-state` |
| `make up`: `container name ... is already in use` | another stack with containers named `loki`, `grafana`, `tempo` is running | stop it, or remove those containers |
| `make up` fails on `node-exporter` | Docker Desktop refuses its mount of `/` | the Makefile already skips it with `--scale node-exporter=0` |
| `make ask STEP=1` stops with `no step 1` | steps are two digits | `STEP=01` |
