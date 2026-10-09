"""Measure the agent before believing it.

Three nested levels, each inside the previous one:

    L1  the backend accepted the query          -> the mechanics work
    L2  the query returned data                 -> it looked in a place that exists
    L3  it returned the SAME data as the gold   -> it answered the actual question

L1 and L2 are cheap to pass and easy to fool yourself with. L3 is the one that
matters: a question about p95 latency answered with average latency passes L1
and L2 and fails L3, which is exactly right.

Two rules learned the hard way, both worth copying:

* **Fixed denominator.** Questions where the agent bails out with the sentinel
  stay in the denominator. Dropping them makes the score go up while the agent
  gets worse at answering.
* **A sentinel question in the set.** One question that nothing can answer,
  where saying "no tool covers this" is the correct behaviour. Without it you
  are only measuring the happy path.

    python bench/run.py 03        # the step to measure: 01, 02, 03 or 04
"""

from __future__ import annotations

import csv
import os
import pathlib
import sys
import time

import yaml
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv()

from bench.steps import load_step  # noqa: E402
from common import backends, tools  # noqa: E402

WINDOW_MINUTES = 10


def _result_set(backend: str, query: str, start: int, end: int) -> set | None:
    """Run a query and reduce it to something comparable.

    Log lines and metric series are compared as sets: the agent may legitimately
    order or page them differently, and that is not a difference worth failing.
    """
    if not query:
        return None
    result = backends.run_loki(query, start, end, limit=1000) if backend == "loki" \
        else backends.run_mimir(query, start, end)
    if not result.get("ok"):
        return None
    return set(result["sample"]) if result["hits"] else set()


def score(run, question: dict, start: int, end: int) -> dict:
    state = run(question["question"], start, end, direct=True)
    findings = state.get("findings") or []
    last = findings[-1] if findings else {}

    row = {
        "id": question["id"],
        "expected_backend": question["backend"],
        "tool": last.get("tool", "-"),
        "query": last.get("query") or "",
        "l1": 0, "l2": 0, "l3": 0,
        "note": "",
    }

    # The sentinel question: the right answer is to decline, and that is L3.
    if question["backend"] == "sentinel":
        declined = last.get("sentinel") or last.get("tool") == tools.SENTINEL
        row.update(l1=1, l2=1, l3=int(bool(declined)),
                   note="correctly declined" if declined else "answered a question it cannot answer")
        return row

    if not last or last.get("sentinel"):
        row["note"] = "the agent declined a question that was answerable"
        return row

    row["l1"] = int(bool(last.get("ok")))
    row["l2"] = int(bool(last.get("hits")))
    if not row["l1"]:
        row["note"] = "the backend refused the query"
        return row
    if not row["l2"]:
        row["note"] = "valid query, no data"
        return row

    gold = _result_set(question["backend"], question["gold"], start, end)
    got = _result_set(question["backend"], last["query"], start, end)
    if gold is None or got is None:
        row["note"] = "could not compare (backend refused one of the two)"
        return row
    row["l3"] = int(gold == got)
    if not row["l3"]:
        row["note"] = f"different result set (gold: {len(gold)}, agent: {len(got)})"
    return row


def main(step: str = "03") -> None:
    name, run = load_step(step)
    path = pathlib.Path(__file__).parent / "questions.yaml"
    questions = yaml.safe_load(path.read_text())
    end = int(time.time())
    start = end - WINDOW_MINUTES * 60

    print(f"Step: {name}")
    print(f"Model: {os.environ.get('AGENT_MODEL', 'mistral/codestral-2508')}")
    print(f"Window: last {WINDOW_MINUTES} minutes\n")

    rows = []
    for question in questions:
        row = score(run, question, start, end)
        rows.append(row)
        mark = "OK " if row["l3"] else "   "
        print(f"  {mark} {row['id']:24} L1={row['l1']} L2={row['l2']} L3={row['l3']}  {row['note']}")

    total = len(rows)
    passed = sum(r["l3"] for r in rows)
    print(f"\nL3: {passed}/{total} (fixed denominator — declining still counts as a miss "
          f"unless declining was correct)")

    out = ROOT / f"results_bench_{step}.csv"
    with out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Per-question detail in {out.name}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "03")
