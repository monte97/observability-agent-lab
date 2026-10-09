# Observability agent lab

An LLM agent that answers questions about a running system by querying Loki,
Tempo and Mimir — built around one idea:

> **Don't let the model write the query. Let it pick from queries you already
> know are valid.**

The parameters it may choose are an `enum` filled with what the system was
found to contain *a moment ago*. A service name that does not exist is not
unlikely — it is unsayable. The query string itself is composed by code the
model never sees.

This is a **lab**, not a product: ~900 lines you can read in one sitting, meant
to show the shape of the thing. It runs against
[iot-observability-demo](https://github.com/monte97/iot-observability-demo),
which gives you a real Kafka pipeline with real telemetry in a few commands.

It is the companion code of the talk **"L'incidente non parla PromQL"**
(DevFest Milano 2026): [talk page](https://montelli.dev/talks/incidente-non-parla-promql/)
and [slides (PDF, Italian)](https://montelli.dev/files/talk-promql-devfest-milano-2026.pdf).
The slides also show a coordinator and a team of agents: that system is not in
this repo. This one is the single-agent skeleton they grew from: same loop,
same guardrails, none of the diagnostic rules (see [What is not here](#what-is-not-here)).

## From the slides to the code

The talk is built on two systems. The one on stage is the full system: a
coordinator, specialist agents, the diagnostic rules, and the numbers on the
slides were measured on it. It is private. This repo is the second one,
written from scratch to show the principle, and you can run it yourself.

Slide titles are in Italian, as in the deck. **Here** means you will find it
in this repo; **Partly** means the idea is here in a simpler form; **Not
here** means it exists only in the system on stage.

**Act I and II: from the question to the agent**

| Slide | What it says | In this repo |
|---|---|---|
| *Tre segnali, una lingua* · *Raccoglie, standardizza, instrada* | logs, metrics, traces, OpenTelemetry | **Here**, as the observed system: `stack/` ([iot-observability-demo](https://github.com/monte97/iot-observability-demo)) |
| *Dati veri, domanda sbagliata* | letting the model write the query fails, even with the real names in the prompt | **Here** as the argument ([Why not just ask the model](#why-not-just-ask-the-model-for-the-query)). The 20-question measurement on the slide comes from the full system |
| *Dallo scrivere allo scegliere* | the model picks, the code writes the query | **Here**, the core: `tools.py`, `loki_schema` / `mimir_schema` (runtime enums) and `loki_compose` / `mimir_compose` |
| *Un ruolo, i suoi strumenti, il suo vocabolario* | role, tools, and a vocabulary discovered from the backends | **Partly**: `discovery.py` builds the vocabulary from Loki and Mimir, never calling the model. One agent with `loki_query`, `mimir_query` and the sentinel; no Tempo, no dashboard panels, no documentation |

**Act III: the team**

| Slide | What it says | In this repo |
|---|---|---|
| *Perché non un agente solo?* | too many options, context rot, one prompt for every role | **Not here**: this repo is that single agent, kept small on purpose |
| *Quando indago, mi faccio quattro domande* | hypotheses, choose, weigh, synthesize | **Here**: `graph.py`, `node_hypotheses`, `node_choose`, `node_weigh`, `node_synthesize` |
| *Nodi e archi* · *Lo stato: la memoria condivisa* | LangGraph nodes, conditional edges, shared state | **Here**: `graph.py`, `State` and `build_graph` |
| *I quattro pezzi, in un grafo* | discover, meta, hypotheses, choose the specialists, execute, process, weigh, synthesize | **Partly**: the same chain without `meta` and without specialists: `choose` picks one of the tools |
| *Quando fermarsi lo decide il codice* | the router: another cycle or close, at confidence 0.7 or three cycles | **Here**: `route_after_weigh`, `CONFIDENCE_THRESHOLD = 0.7`, `MAX_CYCLES = 3`. Empty evidence buys another cycle; the "single source" rule is not here |
| *Un giro: un'ipotesi, un agente* · *Una causa confermata. Le altre restano aperte.* | weighing without discarding what was never investigated (`non indagata`) | **Partly**: `node_weigh` has a different guardrail, an empty result discards its hypothesis whatever the model scored. The `non indagata` marker is in the full system only |
| *Un sistema che non può dire «non lo so» dirà qualcos'altro* | a declared way out | **Here**: `tools.py`, `SENTINEL`; try `make ask Q="how many users abandoned their shopping cart today?"` |
| *Dove siamo arrivati* | 2/20 → 11/20 | **Not here**: measured on the full system. This repo has its own bench, five questions, L3 4/5 ([Measure it](#measure-it-before-believing-it)) |
| *La query è giusta. Il posto no.* | the coordinator sends the question to the wrong agent | **Not here**: there is only one agent |

**Act IV: coordinating**

| Slide | What it says | In this repo |
|---|---|---|
| From *E se il primo indizio fosse il sintomo?* to *Da zero a trentatré* | first-that-holds vs the team, `Send` and reducers, `Command`, the dependency rule, 0 → 7 → 33/33 | **Not here**: all of Act IV is the full system |

**The incident and the close**

| Slide | What it says | In this repo |
|---|---|---|
| *Un servizio fermo, due segnali* | store stopped: no logs from store, normalizer still writing to it | **Here** as a failure you can cause: `make incident` ([Reproduce the demo](#reproduce-the-demo)) |
| *Chiedo in italiano, risponde in italiano* | the system answers "store is down", confidence 1 | **Not here**: on the same failure this repo answers *inconclusive*. Turning silence into evidence takes the rules in [What is not here](#what-is-not-here) |
| *L'affidabilità sta in quello che il modello non ha il permesso di fare.* | the code writes the query, the names come from the backends, the router decides when to stop | **Here**: `tools.py`, `discovery.py`, `route_after_weigh` in `graph.py` |

---

## Why not just ask the model for the query?

Because it was measured, and it doesn't work. Given the question and the
**real topology in the prompt**, a good model still produced:

```
{service="normalizer"}                    → 0 results   (the label is service_name)
{ resource.service.name="your-service-name" }  ← copied the tutorial placeholder
sum by (operation) (rate(http_requests_total[5m]))  → 0 results  (metric doesn't exist)
```

The last one is the dangerous kind: syntactically perfect, accepted by the
backend, and empty. **A parse error you notice. An empty answer looks like an
answer.** Giving the model better information does not fix this reliably —
which is why the schema, not the prompt, is where the constraint belongs.

## Quick start

```bash
git clone --recurse-submodules https://github.com/monte97/observability-agent-lab
cd observability-agent-lab

make setup                  # venv + dependencies, then put your key in .env
make up                     # bring the observed system up (~13 containers)
make check                  # everything green before you go on
make ask Q="which log lines did the store service produce recently?"
```

```
  step 1: loki_query -> 200 results   {service_name=~"store"}

  hypotheses:
       1  which log lines did the store service produce recently?

Answer: The store service is producing log lines related to telemetry.clean —
"stored record from telemetry.clean". The query used was {service_name=~"store"}.
```

`make help` lists everything. The observed system is
[iot-observability-demo](https://github.com/monte97/iot-observability-demo),
pulled in as the `stack/` submodule: a Kafka pipeline with three instrumented
services and a real LGTM stack behind them.

> **On macOS** the Makefile starts the stack with `--scale node-exporter=0`:
> that service mounts `/` in a way Docker Desktop refuses, and one failing
> service stops the whole startup. On Linux you can drop it.

## Pick your model

Every call goes through [LiteLLM](https://docs.litellm.ai/docs/providers), so
the provider is yours to choose. Two routes, set in `.env`:

| You want | `AGENT_MODEL` | Also set |
|---|---|---|
| A hosted provider (Mistral, OpenAI, Anthropic, Gemini, …) | `provider/model`, e.g. `mistral/codestral-2508` | that provider's own key: `MISTRAL_API_KEY`, `OPENAI_API_KEY`, … |
| A gateway or a local server (LM Studio, Ollama, vLLM) | the bare model name, no slash | `LLM_BASE_URL`, plus `LLM_API_KEY` if the endpoint wants one |

Mixing the two routes is the one configuration mistake that costs an
afternoon: a `provider/model` name never goes to `LLM_BASE_URL`. See the
comment in `llm.py`.

The one hard requirement: the model must support **required tool calls**
(`tool_choice="required"`). The agent never accepts prose where a tool call is
expected, so a model that cannot do this fails loudly instead of guessing.

Verified on 2026-10-09, from a clean clone, on the bench below:

| Route | Model | `make ask` | Bench L3 |
|---|---|---|---|
| Hosted, native | `mistral/codestral-2508` | answers, 7 s | 4/5 |
| Local, no key | `qwen3.5-4b-mlx` in LM Studio | answers or stays inconclusive, 20-50 s | 4/5 |

A small local model is slower and less sure of itself, but it keeps to the
guardrails: when it is not confident it says *inconclusive*, it does not invent.

## Things to try

`make examples` runs the first group below, one after another, in well under a
minute. The full list is in `demo-questions.txt`.

**It answers** — one step each, 4-6 seconds:

```bash
make ask Q="show me what store is writing"
make ask Q="is device-gateway logging anything?"
make ask Q="how many log lines has normalizer produced?"
make ask Q="which log lines did the store service produce recently?"
```

> Based on the observed data, the normalizer service has produced at least 200
> log lines. The first few include normalized device IDs dev-003, dev-004 and
> dev-005. The query used was `{service_name=~"normalizer"}`.

Note what is *not* in that answer: no invented numbers, and the query is there
so you can run it yourself.

**It declines** — because nothing here can answer:

```bash
make ask Q="how many users abandoned their shopping cart today?"
```

> No tool of mine covers this question — it goes beyond what this stack exposes.

**It looks, finds nothing, and refuses to conclude** — on a healthy system
there are no errors to find:

```bash
make ask Q="are there any errors anywhere?"
```

Three cycles, every hypothesis discarded by the empty-evidence guardrail, and
an explicit *inconclusive*. Three different outcomes, three different pieces of
the design — and none of them is the model deciding to be careful.

## Reproduce the demo

Four commands, and the interesting part is the last one.

```bash
make stack-state MINUTES=2        # what the agent can see right now
make ask MINUTES=2 Q="which log lines did the store service produce recently?"

make incident                     # stop the consumer that writes to MongoDB
sleep 150                         # let its last logs fall out of the 2-minute window

make ask MINUTES=2 Q="does the store service have any recent log lines?"
make healthy                      # put it back
```

Before the incident the agent answers and cites the query it ran. After it, the
same question gets:

```
  step 3: loki_query -> 0 results   {service_name=~"store"}

  hypotheses:
       0  does the store service have any recent log lines? [discarded]

Answer: Inconclusive: no hypothesis reached the confidence threshold (0.7).
```

**That is the honest ending, and it is the point.** The service is down, the
query is correct, and the result is empty — so the empty-evidence guardrail
discards the hypothesis and the agent refuses to conclude. It does not invent a
cause, which in triage is the right behaviour and is why the guardrail exists.

It is also exactly where this lab stops. Turning that silence *into evidence* —
"nothing from store for two minutes, while normalizer keeps publishing to the
channel store is documented as consuming, therefore store is down" — takes
rules that are not here: see [What is not here](#what-is-not-here).

## What's inside

| File | Lines | What it does |
|---|---|---|
| `discovery.py` | ~80 | Asks the backends what exists. **Never calls the model.** |
| `tools.py` | ~130 | The schemas (with runtime enums) and the query composers. **The core idea.** |
| `backends.py` | ~80 | Runs the composed query, reduces the payload to one readable fact. |
| `graph.py` | ~440 | The LangGraph loop: hypotheses → choose → execute → weigh → synthesize. |
| `llm.py` | ~180 | The only place that talks to the model. Four call sites, all visible.
Run it directly for its self-check. |
| `bench/` | ~140 | Five questions with gold queries, scored L1/L2/L3. |
| `Makefile` | ~80 | Every command you need: setup, up, check, ask, examples, bench, incident. |
| `demo-questions.txt` | — | Questions to try, grouped by what they show. |
| `stack/` | — | The observed system, as a submodule. Not part of the agent. |

### The loop

```
discover → hypotheses → choose → execute → process → weigh ─┬─→ synthesize
                          ^                                 │
                          └─────────────────────────────────┘
```

Seven nodes; the model is called in **four** of them (`hypotheses`, `choose`,
`weigh`, `synthesize`) — and in direct mode `hypotheses` sits out, which leaves
three. Discovery, query composition, execution, reading the result and the
decision to keep going are plain code.

What makes this more than a `for` loop around a chat call is the **exit
condition**: the loop stops when a hypothesis crosses the confidence threshold,
or when the evidence says it cannot — not after a fixed number of turns.

### Two modes, and the difference matters

- **direct** (default): the question *is* the hypothesis, and `hypotheses` does
  not call the model at all. Use it for questions that name what to look at —
  *"does X have errors?"*.
- **scenario** (`--scenario`): the model proposes 2-4 candidate causes and the
  loop investigates them. Use it for questions that describe a symptom —
  *"the dashboard is frozen, why?"*.

Sending a direct question through scenario mode makes the model invent causes
nobody asked about. This lab learned that the boring way: the bench scored
**0/5** until direct mode existed, and 4/5 right after — the agent was fine, the
mode was wrong.

### Three guardrails worth stealing

1. **The runtime enum** (`tools.py`). The constraint lives in the schema, not in
   the prompt. Prompts persuade; schemas guarantee.
2. **The empty-evidence rule** (`graph.py`, `node_weigh`). If the query meant to
   prove a hypothesis came back empty, the code discards that hypothesis no
   matter what confidence the model assigned. *The model proposes, the code
   disposes.*
3. **The declared way out** (`tools.py`, `SENTINEL`). "No tool of mine answers
   this" is offered as a tool. Without it, the model picks the least wrong
   option instead of admitting the gap — and in triage, *"I don't know"* is a
   valid answer while *"probably the database"* said confidently is not.
   Picking it ends the run: asking an unanswerable question a second time
   only gives the model another chance to answer it badly.

## Measure it before believing it

```bash
.venv/bin/python bench/run.py
```

```
  OK  logs-of-a-service        L1=1 L2=1 L3=1
  OK  filtered-by-level        L1=1 L2=1 L3=1
  OK  logs-of-two-services     L1=1 L2=1 L3=1
      jvm-classes              L1=1 L2=1 L3=0  different result set (gold: 1, agent: 1)
  OK  not-covered              L1=1 L2=1 L3=1  correctly declined

L3: 4/5
```

Three nested levels: **L1** the backend accepted the query, **L2** it returned
data, **L3** it returned the *same* data as a gold query written by hand.

L3 is the one that matters. `jvm-classes` above fails it while passing L1 and
L2: the agent picked a plausible JVM metric that is not the one the question
asked about. That failure is left in the repo on purpose — it is the whole
reason L3 exists.

Two rules the bench enforces, both learned by getting them wrong first:

- **Fixed denominator.** A question the agent declines still counts against it.
  Drop those and your score climbs while the agent gets worse.
- **One unanswerable question in the set**, where declining *is* the correct
  answer. Otherwise you are only measuring the happy path.

## What is not here
<a name="what-is-not-here"></a>

This lab is the skeleton. The diagnostic rules that make an agent like this
useful on a real system — deciding when silence is itself evidence, telling a
stopped service from an idle one, knowing when a single source is not enough —
came out of weeks of measurement on production-shaped systems, and are not part
of it. What is here is the shape and the method; those are the parts worth
copying anyway.

## License

MIT.
