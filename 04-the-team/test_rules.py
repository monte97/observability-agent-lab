"""The parts of steps 03 and 04 that are plain code, checked without a model.

    .venv/bin/python 04-the-team/test_rules.py
"""

import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import graph as team  # noqa: E402  (04)

spec = importlib.util.spec_from_file_location("graph03", HERE.parent / "03-the-graph" / "graph.py")
coordinator = importlib.util.module_from_spec(spec)
sys.modules["graph03"] = coordinator
spec.loader.exec_module(coordinator)

DEPS = [{"from": "device-gateway", "to": "normalizer"}, {"from": "normalizer", "to": "store"}]

# Discovery sorts the freshest silence first: with normalizer stopped, store went
# quiet a moment later, so step 03 starts from store. The rule puts normalizer first.
muted = [{"service": "store", "last_seen": 200}, {"service": "normalizer", "last_seen": 190},
         {"service": "keycloak", "last_seen": 100}]
assert [m["service"] for m in team.order_by_rule(muted, DEPS)] == ["normalizer", "store", "keycloak"]
assert team.chains(muted, DEPS) == [{"from": "normalizer", "to": "store"}]
# One silent service: no chain, nothing to reorder.
assert team.order_by_rule(muted[:1], DEPS) == muted[:1] and team.chains(muted[:1], DEPS) == []
# Three in a row: the head of the pipeline first.
three = [{"service": s, "last_seen": 0} for s in ("store", "normalizer", "device-gateway")]
assert [m["service"] for m in team.order_by_rule(three, DEPS)] == ["device-gateway", "normalizer", "store"]
print("rule: ok")

# The trigger: a chain, or two hypotheses above the threshold.
state = {"topology": {"chains": []}, "hypotheses": [{"confidence": 0.9}, {"confidence": 0.8}]}
assert team.trigger(state) == "two hypotheses above the threshold"
state["hypotheses"][1]["confidence"] = 0.5
assert team.trigger(state) == ""
state["topology"]["chains"] = DEPS[1:]
assert team.trigger(state) == "chain normalizer -> store"
print("trigger: ok")

# Silence is evidence only for the code's hypothesis about that very service.
h = {"silent": True, "service": "store"}
quiet = {"tool": "loki_query", "ok": True, "hits": 0, "services": ["store"]}
assert coordinator.is_silence(h, quiet)
assert not coordinator.is_silence({"text": "store has a bug"}, quiet)
assert not coordinator.is_silence(h, {**quiet, "services": ["normalizer"]})
assert not coordinator.is_silence(h, {**quiet, "ok": False})
assert not coordinator.is_silence(h, {**quiet, "level": "error"})   # "no errors" is not silence
print("silence: ok")

# Guardrails: what nobody looked at keeps its score; an empty result discards.
hs = [{"text": "a", "confidence": 0.5, "investigated": True}, {"text": "b", "confidence": 0.5}]
out = coordinator.apply_guardrails(hs, {0: 0.9, 1: 0.0}, 0, empty=False, silence=False)
assert out[0]["confidence"] == 0.9 and out[1]["confidence"] == 0.5 and out[1]["not_investigated"]
out = coordinator.apply_guardrails(hs, {0: 0.9}, 0, empty=True, silence=False)
assert out[0]["discarded"] and out[0]["confidence"] == 0
out = coordinator.apply_guardrails(hs, {0: 0.9}, 0, empty=True, silence=True)
assert not out[0].get("discarded") and out[0]["confidence"] == 0.9
# A silent-service hypothesis is not discarded by a badly aimed empty query.
silent = [{"text": "store is down", "silent": True, "confidence": 0.5, "investigated": True}]
assert not coordinator.apply_guardrails(silent, {0: 0.2}, 0, empty=True, silence=False)[0].get("discarded")
# ...and it cannot be confirmed by anything but its own silence.
assert coordinator.apply_guardrails(silent, {0: 0.9}, 0, empty=False, silence=False)[0]["confidence"] < 0.7
seen = [{**silent[0], "silence_seen": True}]
assert coordinator.apply_guardrails(seen, {0: 0.9}, 0, empty=True, silence=True)[0]["confidence"] == 0.9
print("guardrails: ok")

# The team's tie: equal evidence, the rule picks the one upstream.
topology = {"muted": [{"service": "normalizer"}, {"service": "store"}]}
hyps = [{"service": "store"}, {"service": "normalizer"}]
cmd = team.node_collect({"round": 1, "topology": topology, "hypotheses": hyps,
                         "results": [{"index": 0, "round": 1, "confidence": 0.9},
                                     {"index": 1, "round": 1, "confidence": 0.9}]})
assert cmd.update["verdict"]["chosen"] == 1, cmd
# Nobody convinced in the first round: one replica.
cmd = team.node_collect({"round": 1, "topology": topology, "hypotheses": hyps,
                         "results": [{"index": 0, "round": 1, "confidence": 0.3}]})
assert cmd.goto == "spawn"
print("team: ok")
