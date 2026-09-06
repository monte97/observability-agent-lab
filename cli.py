"""Ask the agent a question.

    python cli.py "does the store service have errors?"
    python cli.py --minutes 30 "which services are logging right now?"
"""

from __future__ import annotations

import argparse
import sys
import time

from dotenv import load_dotenv

load_dotenv()

import graph  # noqa: E402  (after load_dotenv, so the env is populated)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question")
    parser.add_argument("--minutes", type=int, default=10,
                        help="observation window in minutes (default: 10)")
    parser.add_argument("--model", default=None, help="override AGENT_MODEL")
    parser.add_argument("--scenario", action="store_true",
                        help="let the model generate hypotheses (default: the question is "
                             "the hypothesis)")
    args = parser.parse_args(argv)

    end = int(time.time())
    start = end - args.minutes * 60

    state = graph.run(args.question, start, end, args.model, direct=not args.scenario)

    print()
    for i, finding in enumerate(state.get("findings") or [], start=1):
        source = finding.get("query") or "(sentinel)"
        print(f"  step {i}: {finding['tool']} -> {finding['hits']} results   {source}")
    hypotheses = state.get("hypotheses") or []
    if hypotheses:
        print("\n  hypotheses:")
        for h in sorted(hypotheses, key=lambda x: x.get("confidence", 0), reverse=True):
            flag = " [discarded]" if h.get("discarded") else ""
            print(f"    {h.get('confidence', 0):>4}  {h['text']}{flag}")
    print(f"\nAnswer: {state.get('answer', '(none)')}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
