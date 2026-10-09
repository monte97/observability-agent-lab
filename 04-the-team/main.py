"""Step 04: the team, behind a trigger, and the rule of who comes first.

    make ask STEP=04 Q="which log lines did the store service produce recently?"
    make ask STEP=04 MINUTES=2 Q="data no longer reaches MongoDB, what is going on?"
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import graph  # noqa: E402
from common.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(graph.run, __doc__))
