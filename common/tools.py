"""The idea this lab exists to show.

Two functions per data source, and the split between them is the whole
argument:

* ``*_schema`` declares what the model may ask for. The allowed values are an
  ``enum`` filled with what ``discovery`` just found in the system. A service
  name that does not exist is not unlikely — it is *unsayable*.
* ``*_compose`` turns those parameters into a query string. The model never
  sees the query language, so it cannot get its syntax wrong.

The usual approach is the opposite: let the model write LogQL/PromQL, then try
to repair what comes out. Repairing hallucinated output downstream is harder
than making it impossible upstream.

Concepts in this file:

* tool calling: the model answers with a tool name and JSON arguments that
  follow the JSON Schema we declare (`*_schema`);
* `enum` as a closed vocabulary, filled at runtime (`loki_schema`, `mimir_schema`);
* LogQL: stream selector `{label=~"regex"}` plus a `| detected_level="..."`
  label filter (`loki_compose`);
* PromQL: metric name, label matchers, `rate()` over a window, `sum()`
  (`mimir_compose`);
* the sentinel: an explicit "none of my tools" answer (`sentinel_schema`).
"""

from __future__ import annotations

LOG_LEVELS = ("error", "warn", "info", "debug")

# The declared "not covered" answer. An agent that cannot say "no tool of mine
# answers this" will answer anyway, with something plausible.
SENTINEL = "no_tool_covers_this"


def loki_schema(services: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "loki_query",
            "description": "Read recent log lines for one or more services.",
            "parameters": {
                "type": "object",
                "properties": {
                    "services": {
                        "type": "array",
                        "items": {"enum": list(services)},  # <- discovered at runtime
                        "description": "Services to read logs from.",
                    },
                    "level": {
                        "enum": list(LOG_LEVELS),
                        "description": "Keep only lines at this level. Omit to see everything.",
                    },
                },
                "required": ["services"],
            },
        },
    }


def loki_compose(services: list[str], level: str | None = None) -> str:
    """Build LogQL. The model never sees this string, only its parameters."""
    if not services:
        raise ValueError("loki_query needs at least one service")
    # A stream selector: {service_name=~"a|b"} picks the log streams whose label
    # matches the regex (=~ is regex match, = is exact). It is the mandatory
    # first part of every LogQL query.
    query = '{service_name=~"%s"}' % "|".join(services)
    if level:
        # `|` starts a pipeline stage applied to the selected lines. Loki derives
        # the label detected_level from each line, so a label filter can keep
        # one level.
        query += f' | detected_level="{level}"'
    return query


def mimir_schema(metrics: list[str], labels: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "mimir_query",
            "description": "Read a metric, optionally filtered by label and aggregated.",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {
                        "enum": list(metrics),  # <- discovered at runtime
                        "description": "The metric to read.",
                    },
                    "label_filters": {
                        "type": "object",
                        "description": "Label/value pairs to filter on. Labels must exist: "
                                       + ", ".join(labels[:20]) + ("…" if len(labels) > 20 else ""),
                    },
                    "rate_window": {
                        "type": "string",
                        "description": "Window for rate(), e.g. '5m'. Omit for an instant read.",
                    },
                },
                "required": ["metric"],
            },
        },
    }


def mimir_compose(metric: str, label_filters: dict | None = None,
                  rate_window: str | None = None) -> str:
    """Build PromQL from parameters the model chose from an enum."""
    # A PromQL selector is the metric name plus optional label matchers:
    # http_requests_total{job="store"} picks the series with that label value.
    selector = metric
    if label_filters:
        pairs = ",".join(f'{k}="{v}"' for k, v in sorted(label_filters.items()))
        selector = f"{metric}{{{pairs}}}"
    if rate_window:
        # rate(x[5m]) is the per-second increase of a counter averaged over the
        # last 5 minutes (a range vector). It gives one value per series; sum()
        # adds them into a single number across all the labels.
        return f"sum(rate({selector}[{rate_window}]))"
    return selector


def sentinel_schema() -> dict:
    """The explicit way out, offered as a tool like any other.

    Without it, "none of my tools answers this question" has no shape the model
    can produce, and the model will pick the least wrong tool instead.
    """
    return {
        "type": "function",
        "function": {
            "name": SENTINEL,
            "description": "Choose this when no other tool can answer the question. "
                           "Saying so is a valid answer.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def schemas(topology: dict) -> list[dict]:
    """Everything the model may choose from, for this question, right now."""
    return [
        loki_schema(topology["services"]),
        mimir_schema(topology["metrics"], topology["labels"]),
        sentinel_schema(),
    ]
