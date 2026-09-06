"""What exists in the observed system, asked to the system itself.

This module never talks to the model. It queries the backends and returns
plain lists of names. Everything the agent is later *allowed* to say comes
from here — that is the whole point of the lab.
"""

from __future__ import annotations

import os

import requests

TIMEOUT = 30


def _get(url: str, params: dict) -> dict | None:
    """GET returning parsed JSON, or None when the backend is unhappy.

    A backend that answers 500 is a fact about the environment, not a crash:
    callers degrade instead of exploding.
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
    """Service names Loki knows about in the window."""
    payload = _get(
        f"{os.environ['LOKI_URL']}/loki/api/v1/label/service_name/values",
        {"start": start, "end": end},
    )
    return sorted((payload or {}).get("data") or [])


def mimir_metrics(start: int, end: int) -> list[str]:
    """Metric names Mimir knows about in the window."""
    payload = _get(
        f"{os.environ['MIMIR_URL']}/prometheus/api/v1/label/__name__/values",
        {"start": start, "end": end},
    )
    return sorted((payload or {}).get("data") or [])


def mimir_labels(start: int, end: int) -> list[str]:
    """Label names available for filtering, minus __name__ (that is the metric)."""
    payload = _get(
        f"{os.environ['MIMIR_URL']}/prometheus/api/v1/labels",
        {"start": start, "end": end},
    )
    return sorted(l for l in ((payload or {}).get("data") or []) if l != "__name__")


def topology(start: int, end: int) -> dict:
    """Everything the agent is allowed to name, discovered at runtime.

    Called once per question. The system it observes may change between two
    questions, and the agent must follow it — a topology written by hand in a
    prompt is a topology that goes stale.
    """
    return {
        "services": loki_services(start, end),
        "metrics": mimir_metrics(start, end),
        "labels": mimir_labels(start, end),
    }
