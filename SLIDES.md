# The talk, in this repo

Companion of [README.md](README.md). Commands to replay the slides, and a map
from every slide of [the deck (PDF, Italian)](https://montelli.dev/files/talk-promql-devfest-milano-2026.pdf)
to the code.

## Run the talk, slide by slide

Each pair of commands below is a slide you can run.

**The model writes, the model picks** (slides "Dati veri, domanda sbagliata",
"Dallo scrivere allo scegliere"). Same question, steps 01 and 02:

```bash
make ask STEP=01 Q="which log lines did the store service produce recently?"
make ask STEP=02 Q="which log lines did the store service produce recently?"
```

Step 01 writes `{service="store"}` and concludes there are no logs. Step 02
can only name a label value that discovery found, and reads 139 lines.

**Saying "I don't know"** (slide "Un sistema che non può dire «non lo so» dirà
qualcos'altro"). A question nothing in this stack can answer:

```bash
make ask STEP=02 Q="how many users abandoned their shopping cart today?"
make ask STEP=03 Q="how many users abandoned their shopping cart today?"
```

Step 02 has no way out, so it queries a metric with an invented label, gets
nothing, and answers that no users abandoned their cart. Step 03 offers the
sentinel as a tool and answers *No tool of mine covers this question*.

**The first that holds** (slide "E se il primo indizio fosse il sintomo?").
Stop `store`, wait for its last logs to leave a 2-minute window, ask step 03:

```bash
make incident && sleep 150
make scenario STEP=03 MINUTES=2 Q="Data no longer reaches MongoDB. What is going on?"
make healthy
```

Discovery sees `store` silent, the code puts "store is down" first among the
hypotheses, the logs specialist reads store's logs and finds them empty, and
that silence is the evidence. Now stop `normalizer` instead:

```bash
make incident-upstream && sleep 150
make compare SERVICE=normalizer RUNS=3      # steps 03 and 04, side by side
make healthy
```

Both services are silent now. Step 03 starts from the freshest silence, which
is store (it went quiet a moment after normalizer), confirms it and closes:
the symptom, named as the cause. Step 04 knows from `dependencies.yaml` that
normalizer feeds store, puts it first, and the chain triggers the team:

```
  hypotheses:
       1  normalizer is down: it wrote log lines until 7 minutes ago and nothing since [from the code]
       0  store is down: it wrote log lines until 7 minutes ago and nothing since [from the code]
       0  The device-gateway service is not sending data to the normalizer service. [discarded]
  route: team (trigger: chain normalizer -> store; 1 round(s); one hypothesis held)

Answer: normalizer is down: it wrote log lines until 7 minutes ago and nothing since.
(Query: {service_name=~"normalizer"})
  model calls: 12 · 4.8s
```

## Where each slide lives

Slide titles are in Italian, as in the deck. **Here** means the repo has it;
**Partly** means it is here in a simpler form; **Stage only** means it exists
only in the system shown on stage.

| Slide | In this repo |
|---|---|
| *Tre segnali, una lingua* · *Raccoglie, standardizza, instrada* | **Here**, as the observed system: `stack/` |
| *La cosa che si prova per prima* · *Dati veri, domanda sbagliata* | **Here**: step 01, with and without `WITH_NAMES=1`. The 20-question numbers on the slide come from the stage system |
| *Dallo scrivere allo scegliere* | **Here**: step 02, `common/tools.py` (`*_schema` and `*_compose`) |
| *Un ruolo, i suoi strumenti, il suo vocabolario* | **Here**: `common/agents.py`, two specialists (logs, metrics), vocabulary from `common/discovery.py`. No Tempo, no dashboard panels, no documentation |
| *Perché non un agente solo?* | **Here** as a contrast: step 02 gives every tool to one agent, step 03 splits them |
| *Quando indago, mi faccio quattro domande* · *Nodi e archi* · *Lo stato: la memoria condivisa* | **Here**: step 03, `node_hypotheses`, `node_choose`, `node_weigh`, `node_synthesize`, `State`, `build_graph` |
| *I quattro pezzi, in un grafo* | **Here**: `discover`, `meta`, `hypotheses`, `choose` (coordinator picks the specialist), `execute` (specialist picks the tool), `process`, `weigh`, `synthesize` |
| *Quando fermarsi lo decide il codice* | **Here**: `route_after_weigh`, threshold 0.7, three cycles. The "single source" rule is stage only |
| *Un giro: un'ipotesi, un agente* · *Una causa confermata. Le altre restano aperte.* | **Here**: the code picks the hypothesis, the model the agent; `apply_guardrails` keeps what nobody investigated as `[not investigated]` |
| *Un sistema che non può dire «non lo so» dirà qualcos'altro* | **Here**: `SENTINEL` in `common/tools.py`, missing on purpose from step 02 |
| *Dove siamo arrivati* | **Partly**: this repo's own bench, above |
| *La query è giusta. Il posto no.* | **Partly**: the coordinator can send a hypothesis to the wrong specialist; step 03 does not detect it |
| *E se il primo indizio fosse il sintomo?* · *Il grafo di adesso* | **Here**: step 03 on `make incident` and `make incident-upstream` |
| *Il primo che regge, o il migliore fra tutti* · *Un agente per ipotesi, tutti insieme* | **Here**: step 04, `send_agents` (`Send`) and `TeamState.results` (reducer) |
| *Un nodo che decide anche dove andare* | **Here**: `node_collect` returns a `Command` (back to `spawn` for one replica, or to the end) |
| *La squadra confronta, ma le prove sono uguali* | **Here**: when two hypotheses hold on equal evidence, `node_collect` lets the rule pick |
| *Chi viene prima, scritto nel codice* | **Partly**: `order_by_rule`, with the edges declared by hand in `dependencies.yaml`; the stage system reads them from the documentation |
| *La squadra, quando la regola non basta* · *La nuova coordinazione, intera* | **Here**: `trigger` and the `team` node in step 04 |
| *Da zero a trentatré* | **Partly**: `make compare`, nine runs instead of 33 |
| *Un servizio fermo, due segnali* · *Chiedo in italiano, risponde in italiano* | **Here**: `make incident`; ask in Italian if you like, the agent answers in kind |
| *Un agente che scrive il post-mortem* | **Stage only**: future work in the talk |
