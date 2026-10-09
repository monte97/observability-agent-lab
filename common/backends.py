"""Running the query the code composed, and turning the payload into a fact.

Split on purpose from `tools.py`: composing a query and executing it are two
different jobs, and only the first one has anything to do with the model.

Concepts in this file:

* Loki `query_range`: LogQL over an interval, start/end in nanoseconds, `limit`
  caps the lines returned (`run_loki`);
* Mimir (Prometheus API) instant query: the value of a PromQL expression at one
  moment (`run_mimir`);
* "backend refused" versus "valid query, zero results": two different facts
  (`run_loki`, `run_mimir`, `to_facts`).
"""

from __future__ import annotations

import os

import requests

TIMEOUT = 30


def run_loki(query: str, start: int, end: int, limit: int = 200) -> dict:
    """Execute LogQL. Returns {ok, hits, sample, query}.

    `ok=False` means the backend refused; `hits=0` with `ok=True` means the
    query was valid and found nothing. Keeping the two apart matters: an empty
    answer looks like an answer, and the agent must be able to tell.
    """
    # start/end arrive as unix seconds; query_range wants nanoseconds. Without a
    # `direction`, Loki answers newest first, so `limit` keeps the newest lines.
    try:
        r = requests.get(
            f"{os.environ['LOKI_URL']}/loki/api/v1/query_range",
            params={"query": query, "start": start * 10**9, "end": end * 10**9, "limit": limit},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        return {"ok": False, "hits": 0, "sample": [], "query": query, "error": str(exc)}
    # Loki answers 4xx for a query it cannot parse or run: that is "refused".
    if r.status_code != 200:
        return {"ok": False, "hits": 0, "sample": [], "query": query, "error": r.text[:200]}

    # A log query returns streams: one per distinct label set, each holding
    # [timestamp, line] pairs. Flatten them. No streams at all is still a valid
    # answer (ok=True, hits=0).
    streams = (r.json().get("data") or {}).get("result") or []
    lines = [v[1] for s in streams for v in s.get("values", [])]
    return {"ok": True, "hits": len(lines), "sample": lines[:5], "query": query}


def run_mimir(query: str, start: int, end: int) -> dict:
    """Execute PromQL (instant query at `end`). Returns {ok, hits, sample, query}.

    An instant query evaluates the expression at one timestamp and gives one
    value per series. A range query (`/query_range`) would give a whole curve
    over an interval; the agent only needs "what is it now".
    """
    try:
        r = requests.get(
            f"{os.environ['MIMIR_URL']}/prometheus/api/v1/query",
            params={"query": query, "time": end},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        return {"ok": False, "hits": 0, "sample": [], "query": query, "error": str(exc)}
    if r.status_code != 200:
        return {"ok": False, "hits": 0, "sample": [], "query": query, "error": r.text[:200]}

    # Each element is one series: its labels ("metric") and one [time, value]
    # sample. Zero series for a valid expression means "no such data".
    series = (r.json().get("data") or {}).get("result") or []
    sample = [f"{s.get('metric', {})} = {s.get('value', ['', ''])[1]}" for s in series[:5]]
    return {"ok": True, "hits": len(series), "sample": sample, "query": query}


def to_facts(result: dict, tool: str) -> str:
    """One readable line out of a payload, for the model to reason on.

    The three branches are three different facts: refused, valid but empty,
    and answered. Collapsing the first two would make a broken query look like
    "nothing is wrong".

    The raw payload never reaches the model: it is large, it is noisy, and it
    invites the model to quote numbers it did not check.
    """
    if not result.get("ok"):
        return f"{tool}: the backend refused the query ({result.get('error', 'unknown error')[:80]})"
    if result["hits"] == 0:
        return f"{tool}: the query was valid and returned nothing ({result['query']})"
    head = " / ".join(str(s)[:120] for s in result["sample"][:3])
    return f"{tool}: {result['hits']} results. First ones: {head}"
