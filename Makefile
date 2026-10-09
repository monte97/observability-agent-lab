# Observability agent lab — everything you need to reproduce the demo.
#
#   make setup             once
#   make up                bring the observed system up
#   make check             is everything ready?
#   make ask STEP=02 Q="…"  ask one step something (01, 02, 03, 04; default 03)
#   make bench STEP=01     measure a step
#
# The observed system lives in stack/ (a submodule: iot-observability-demo).
# It is composed from inside that directory, because its compose files use
# paths relative to their own root.

SHELL := /bin/bash
ROOT  := $(shell pwd)
STACK := $(ROOT)/stack
PY    := $(ROOT)/.venv/bin/python

# node-exporter mounts / with a propagation mode Docker Desktop refuses, and
# one failing service stops the whole startup. On Linux you can drop --scale.
COMPOSE := COMPOSE_PROJECT_NAME=iot-obs-demo docker compose

# The backends, as published by the stack's compose.
ENV := LOKI_URL=http://localhost:3100 TEMPO_URL=http://localhost:3200 \
       MIMIR_URL=http://localhost:9009

MINUTES ?= 10
Q ?= which log lines did the store service produce recently?
STEP ?= 03
STEP_DIR = $(firstword $(wildcard $(ROOT)/$(STEP)-*))
RUNS ?= 3
SERVICE ?= normalizer
check_step = $(if $(STEP_DIR),,$(error no step $(STEP): use 01, 02, 03 or 04))

.PHONY: help setup up down check ask scenario bench test compare incident incident-upstream healthy stack-state examples

help:        ## This list
	@grep -hE '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | column -t -s $$'\t'

setup:       ## Create the venv and install dependencies
	python3 -m venv .venv
	$(ROOT)/.venv/bin/pip install -q -r requirements.txt
	@test -f .env || cp .env.example .env
	@echo "Done. Now put your model key in .env (see the comments in there)."

up:          ## Start the observed system (~13 containers, under a minute)
	cd $(STACK) && $(COMPOSE) up -d --scale node-exporter=0
	@echo "Grafana http://localhost:3000 · Loki :3100 · Tempo :3200 · Mimir :9009"

down:        ## Stop it
	cd $(STACK) && $(COMPOSE) down

check:       ## Is everything ready? (run this before anything else)
	@cd $(STACK) && $(COMPOSE) ps --format '{{.Name}} {{.Status}}' | sort
	@echo
	@for u in http://localhost:3100/loki/api/v1/labels http://localhost:3200/api/echo \
	          http://localhost:9009/prometheus/api/v1/labels; do \
	  printf '  %-52s %s\n' "$$u" "$$(curl -s -m 5 -o /dev/null -w '%{http_code}' $$u)"; done
	@test -x $(PY) && echo "  venv: ok" || echo "  venv: MISSING (make setup)"
	@if grep -qE '^[A-Z_]*API_KEY=.+' .env 2>/dev/null; then echo "  model key: ok"; \
	elif grep -qE '^LLM_BASE_URL=.+' .env 2>/dev/null; then echo "  model key: none (fine for a local server at LLM_BASE_URL)"; \
	else echo "  model key: MISSING in .env"; fi

stack-state: ## What the agent can see right now, and which services are silent
	@cd $(ROOT) && $(ENV) $(PY) -c "import time; from common import discovery; \
	e=int(time.time()); t=discovery.topology(e-$(MINUTES)*60, e); \
	print('services:', t['services']); \
	print('silent  :', [m['service'] for m in t['muted']] or 'none'); \
	print('metrics :', len(t['metrics']), '| labels:', len(t['labels']))"

ask:         ## Ask a step: make ask STEP=02 Q="does store have errors?"
	$(check_step)
	@cd $(ROOT) && $(ENV) $(PY) $(STEP_DIR)/main.py --minutes $(MINUTES) "$(Q)"

scenario:    ## Ask it as a symptom to investigate: make scenario STEP=04 MINUTES=2 Q="…"
	$(check_step)
	@cd $(ROOT) && $(ENV) $(PY) $(STEP_DIR)/main.py --scenario --minutes $(MINUTES) "$(Q)"

examples:    ## Run the questions that answer in one step, one after another
	$(check_step)
	@while IFS= read -r q; do \
	  case "$$q" in ''|\#*) continue;; esac; \
	  echo; echo "### $$q"; \
	  cd $(ROOT) && $(ENV) $(PY) $(STEP_DIR)/main.py --direct --minutes $(MINUTES) "$$q" | tail -4; \
	done < <(sed -n '/it answers, in one step/,/^$$/p' demo-questions.txt)

bench:       ## Measure a step against five gold queries: make bench STEP=01
	$(check_step)
	@cd $(ROOT) && $(ENV) $(PY) bench/run.py $(STEP)

test:        ## The plain-code parts of steps 03 and 04, no model, no stack
	@cd $(ROOT) && $(PY) 04-the-team/test_rules.py && $(PY) -m common.llm

compare:     ## After an incident: make compare SERVICE=normalizer RUNS=3 (steps 03 and 04)
	@cd $(ROOT) && $(ENV) $(PY) bench/incident.py $(SERVICE) $(RUNS) 03 04

incident:    ## Break something: stop store, the consumer that writes to MongoDB
	cd $(STACK) && $(COMPOSE) stop store
	@echo "store stopped at $$(date +%H:%M:%S). Give it ~$(MINUTES) minutes to fall out"
	@echo "of the observation window, or use MINUTES=2 for a shorter one."

incident-upstream: ## Break it one step earlier: stop normalizer, and store goes quiet too
	cd $(STACK) && $(COMPOSE) stop normalizer
	@echo "normalizer stopped at $$(date +%H:%M:%S). store has nothing left to write:"
	@echo "in ~2 minutes both are silent. Then: make compare SERVICE=normalizer"

healthy:     ## Put everything back
	cd $(STACK) && $(COMPOSE) start normalizer store
