"""The same command line for every step, so that two steps are compared on the
same question with the same output.

    make ask STEP=03 Q="does the store service have errors?"
    .venv/bin/python 03-the-graph/main.py --minutes 2 --scenario "data stopped reaching MongoDB"
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Callable

from dotenv import load_dotenv

load_dotenv()

from common import llm  # noqa: E402


def flags(h: dict) -> str:
    out = []
    if h.get("from_code"):
        out.append("from the code")
    if h.get("not_investigated"):
        out.append("not investigated")
    if h.get("discarded"):
        out.append("discarded")
    return f" [{', '.join(out)}]" if out else ""


def main(run: Callable, doc: str, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=doc.strip().splitlines()[0])
    parser.add_argument("question")
    parser.add_argument("--minutes", type=int, default=10,
                        help="observation window in minutes (default: 10)")
    parser.add_argument("--model", default=None, help="override AGENT_MODEL")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--scenario", action="store_true",
                      help="treat the question as a symptom: propose causes and investigate")
    mode.add_argument("--direct", action="store_true",
                      help="treat the question as a direct lookup")
    args = parser.parse_args(argv)

    end = int(time.time())
    start = end - args.minutes * 60
    direct = True if args.direct else False if args.scenario else None

    t0 = time.time()
    try:
        state = run(args.question, start, end, args.model, direct=direct)
    except Exception as exc:  # the provider's own message says what to fix; the stack trace does not
        if type(exc).__module__.split(".")[0] != "litellm":
            raise
        print(f"\nThe model call failed: {' '.join(str(exc).split())[:400]}\n"
              "Check AGENT_MODEL and its key in .env (see .env.example).\n", file=sys.stderr)
        return 1

    print()
    for i, finding in enumerate(state.get("findings") or [], start=1):
        source = finding.get("query") or "(sentinel)"
        who = f"{finding['agent']}: " if finding.get("agent") else ""
        print(f"  step {i}: {who}{finding['tool']} -> {finding['hits']} results   {source}")
    hypotheses = state.get("hypotheses") or []
    if hypotheses:
        print("\n  hypotheses:")
        for h in sorted(hypotheses, key=lambda x: x.get("confidence", 0), reverse=True):
            print(f"    {h.get('confidence', 0):>4}  {h['text']}{flags(h)}")
    if state.get("route"):
        print(f"\n  route: {state['route']}")
    print(f"\nAnswer: {state.get('answer', '(none)')}\n")
    print(f"  model calls: {llm.CALLS} · {time.time() - t0:.1f}s\n")
    return 0
