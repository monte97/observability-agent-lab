"""What exists in the observed system, asked to the system itself.

This module never talks to the model. It queries the backends and returns
plain lists of names. Everything the agent is later *allowed* to say comes
from here — that is the whole point of the lab.

Concepts in this file:

* OpenTelemetry names each service with the resource attribute `service.name`;
  Loki stores it as the label `service_name` (`loki_services`);
* label-values API: list every value a label took in a window. Loki's gives
  the services, Mimir's on `__name__` gives the metric names (`loki_services`,
  `mimir_metrics`);
* Mimir is Prometheus-compatible storage, served under `/prometheus`;
* `query_range` with nanoseconds, `limit` and `direction=backward` (`last_seen`);
* silence as evidence, only in a narrow declared case (`muted`).
"""

from __future__ import annotations

import os

import requests

TIMEOUT = 30


def _get(url: str, params: dict) -> dict | None:
    """GET returning parsed JSON, or None when the backend is unhappy.

    A backend that answers 500 is a fact about the environment, not a crash:
    callers degrade instead of exploding. None means "unreachable or refused";
    an empty list further down means "answered, and has nothing": different facts.
    """
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def loki_services(start: int, end: int) -> list[str]:
    """Service names Loki knows about in the window.

    OpenTelemetry sends each service's `service.name`; Loki exposes it as the
    label `service_name`. This endpoint lists the values that label took between
    start and end (unix seconds), i.e. the services that logged in the window.
    """
    payload = _get(
        f"{os.environ['LOKI_URL']}/loki/api/v1/label/service_name/values",
        {"start": start, "end": end},
    )
    return sorted((payload or {}).get("data") or [])


def mimir_metrics(start: int, end: int) -> list[str]:
    """Metric names Mimir knows about in the window.

    Mimir speaks the Prometheus API. A metric's name is stored as the
    pseudo-label `__name__`, so the values of that label are the metric names.
    """
    payload = _get(
        f"{os.environ['MIMIR_URL']}/prometheus/api/v1/label/__name__/values",
        {"start": start, "end": end},
    )
    return sorted((payload or {}).get("data") or [])


def mimir_labels(start: int, end: int) -> list[str]:
    """Label names available for filtering, minus __name__ (that is the metric).

    Labels are the key=value pairs on a series (`job`, `instance`, ...): the
    dimensions a PromQL selector can filter on.
    """
    payload = _get(
        f"{os.environ['MIMIR_URL']}/prometheus/api/v1/labels",
        {"start": start, "end": end},
    )
    return sorted(l for l in ((payload or {}).get("data") or []) if l != "__name__")


# How far back "it used to log" looks. A service that went quiet ten minutes
# ago must still be nameable, or the agent could not even ask about it.
HISTORY = 3600


def last_seen(service: str, start: int, end: int) -> int | None:
    """Unix time of the newest log line of `service` in the window, or None.

    `query_range` runs a LogQL query over an interval. `{service_name="x"}` is
    a stream selector: it picks every log stream carrying that label. Loki wants
    nanoseconds (hence `* 10**9`). `direction=backward` returns newest first and
    `limit=1` keeps one line, so we get the latest timestamp and nothing more.
    """
    payload = _get(
        f"{os.environ['LOKI_URL']}/loki/api/v1/query_range",
        {"query": '{service_name="%s"}' % service, "start": start * 10**9,
         "end": end * 10**9, "limit": 1, "direction": "backward"},
    )
    streams = ((payload or {}).get("data") or {}).get("result") or []
    stamps = [int(v[0]) for s in streams for v in s.get("values", [])]
    return max(stamps) // 10**9 if stamps else None


def muted(services: list[str], start: int, end: int) -> list[dict]:
    """Services that logged in the last hour and are silent in [start, end].

    Silence is evidence only in this narrow case: the service was logging, then
    stopped, and discovery declares it. A service with no logs at all, or an
    empty result for any other query, proves nothing.

    ponytail: "it used to log, now it does not" is all this knows. It cannot
    tell a stopped service from one with nothing to do: that takes rules this
    lab does not have (see "What is not here" in the README).

    Sorted freshest silence first: the service that went quiet last looks like
    the most recent change. Plausible, and on a pipeline it is backwards (the
    consumer goes quiet a moment after its producer stops). Step 04 fixes it.
    """
    out = []
    for service in services:
        seen = last_seen(service, end - HISTORY, end)
        if seen is not None and seen < start:
            out.append({"service": service, "last_seen": seen})
    return sorted(out, key=lambda m: m["last_seen"], reverse=True)


def topology(start: int, end: int) -> dict:
    """Everything the agent is allowed to name, discovered at runtime.

    Called once per question. The system it observes may change between two
    questions, and the agent must follow it — a topology written by hand in a
    prompt is a topology that goes stale.

    Services come from the last hour, so a service that has just gone quiet is
    still in the vocabulary; `muted` says which ones are quiet right now.
    """
    services = loki_services(end - HISTORY, end)
    return {
        "services": services,
        "metrics": mimir_metrics(start, end),
        "labels": mimir_labels(start, end),
        "muted": muted(services, start, end),
    }
