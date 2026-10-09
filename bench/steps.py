"""Load the `run` of one step by its number, so the bench measures any of them."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_step(step: str):
    """"03" -> ("03-the-graph", run). Graph steps keep `run` in graph.py."""
    folder = next(ROOT.glob(f"{step}-*"), None)
    if folder is None:
        raise SystemExit(f"no step {step}: pick one of "
                         + ", ".join(sorted(p.name[:2] for p in ROOT.glob("0?-*"))))
    path = folder / "graph.py" if (folder / "graph.py").exists() else folder / "main.py"
    name = f"step{step}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module   # LangGraph reads the state's type hints from here
    spec.loader.exec_module(module)
    return folder.name, module.run
