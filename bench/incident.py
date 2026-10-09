"""The same investigation on several steps, after a failure you caused yourself.

    make incident                 # stop store, wait ~2 minutes
    .venv/bin/python bench/incident.py store 3 03 04

    make incident-upstream        # stop normalizer instead
    .venv/bin/python bench/incident.py normalizer 3 03 04

For every (step, question, run): is the cause right, how many model calls, how
many seconds. "Right" means the service named first in the answer is the one
you stopped: "store is down, while normalizer keeps feeding it" names store.
The slides compare the steps the same way (slide "Da zero a trentatré"); the
numbers on the slides come from the system on stage, these from this repo.
"""

from __future__ import annotations

import re
import sys
import time
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from bench.steps import load_step  # noqa: E402
from common import llm  # noqa: E402

SERVICES = ["device-gateway", "normalizer", "store"]
QUESTIONS = [
    "Data no longer reaches MongoDB. What is going on?",
    "The aggregated data stopped updating, yet the gateway accepts the devices' requests. Why?",
    "Measurements sent by the devices no longer end up in the database. What happened?",
]
WINDOW = 120


def first_named(text: str) -> str | None:
    """The service named first, by whole word ("datastore" is not "store")."""
    found = {s: m.start() for s in SERVICES
             if (m := re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", text.lower()))}
    return min(found, key=found.get) if found else None


def main(expected: str, runs: int, steps: list[str]) -> None:
    loaded = [load_step(s) for s in steps]
    rows = []
    for n in range(1, runs + 1):
        for question in QUESTIONS:
            for name, run in loaded:
                end = int(time.time())
                calls, t0 = llm.CALLS, time.time()
                try:
                    answer = run(question, end - WINDOW, end, direct=False).get("answer") or ""
                except Exception as exc:  # one failed run is a data point, not the end of the bench
                    answer = f"(error: {exc})"
                right = first_named(answer) == expected
                rows.append((name, right, llm.CALLS - calls, time.time() - t0))
                print(f"  run {n}  {name:20} {'OK' if right else 'NO'}  {llm.CALLS - calls:3} calls  "
                      f"{time.time() - t0:5.1f}s  {' '.join(answer.split())[:90]}", flush=True)
    print()
    for name, _ in loaded:
        mine = [r for r in rows if r[0] == name]
        print(f"{name:20} right {sum(r[1] for r in mine)}/{len(mine)}  "
              f"calls {sum(r[2] for r in mine) / len(mine):.1f}  "
              f"seconds {sum(r[3] for r in mine) / len(mine):.1f}")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    main(sys.argv[1], int(sys.argv[2]), sys.argv[3:])
