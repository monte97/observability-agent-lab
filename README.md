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

> **Which key.** `.env.example` defaults to `mistral/codestral-2508`, which
> LiteLLM routes to Mistral natively — so the variable it reads is
> `MISTRAL_API_KEY`. Point `AGENT_MODEL` at a bare model name instead (no
> slash) to use an OpenAI-compatible gateway via `LLM_BASE_URL` /
> `LLM_API_KEY`. Mixing the two routes is the one configuration mistake that
> costs an afternoon — see the comment in `llm.py`.
>
> **On macOS** the Makefile starts the stack with `--scale node-exporter=0`:
> that service mounts `/` in a way Docker Desktop refuses, and one failing
> service stops the whole startup. On Linux you can drop it.

Verified from a clean clone on 2026-09-06.

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
| `llm.py` | ~180 | The only place that talks to the model. Three call sites, all visible.
Run it directly for its self-check. |
| `bench/` | ~140 | Five questions with gold queries, scored L1/L2/L3. |
| `Makefile` | ~80 | Every command you need: setup, up, check, ask, examples, bench, incident. |
| `demo-questions.txt` | — | Questions to try, grouped by what they show. |
| `stack/` | — | The observed system, as a submodule. Not part of the agent. |

### The loop

```
discover → hypotheses → choose → execute → weigh ─┬─→ synthesize
                          ^                       │
                          └───────────────────────┘
```

Six nodes; the model is called in **three** of them (`hypotheses`, `choose`,
`weigh`, plus the final prose). Discovery, query composition, execution and the
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
