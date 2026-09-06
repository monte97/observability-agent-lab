"""Running the query the code composed, and turning the payload into a fact.

Split on purpose from `tools.py`: composing a query and executing it are two
different jobs, and only the first one has anything to do with the model.
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
    try:
        r = requests.get(
            f"{os.environ['LOKI_URL']}/loki/api/v1/query_range",
            params={"query": query, "start": start * 10**9, "end": end * 10**9, "limit": limit},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        return {"ok": False, "hits": 0, "sample": [], "query": query, "error": str(exc)}
    if r.status_code != 200:
        return {"ok": False, "hits": 0, "sample": [], "query": query, "error": r.text[:200]}

    streams = (r.json().get("data") or {}).get("result") or []
    lines = [v[1] for s in streams for v in s.get("values", [])]
    return {"ok": True, "hits": len(lines), "sample": lines[:5], "query": query}


def run_mimir(query: str, start: int, end: int) -> dict:
    """Execute PromQL (instant query at `end`). Returns {ok, hits, sample, query}."""
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

    series = (r.json().get("data") or {}).get("result") or []
    sample = [f"{s.get('metric', {})} = {s.get('value', ['', ''])[1]}" for s in series[:5]]
    return {"ok": True, "hits": len(series), "sample": sample, "query": query}


def to_facts(result: dict, tool: str) -> str:
    """One readable line out of a payload, for the model to reason on.

    The raw payload never reaches the model: it is large, it is noisy, and it
    invites the model to quote numbers it did not check.
    """
    if not result.get("ok"):
        return f"{tool}: the backend refused the query ({result.get('error', 'unknown error')[:80]})"
    if result["hits"] == 0:
        return f"{tool}: the query was valid and returned nothing ({result['query']})"
    head = " / ".join(str(s)[:120] for s in result["sample"][:3])
    return f"{tool}: {result['hits']} results. First ones: {head}"
