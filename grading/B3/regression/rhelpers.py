"""Replay helpers for the regression suite (self-contained; does not import tests/)."""

import functools
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAULTS = os.path.join(ROOT, "scenarios", "faults")


def faults(name):
    return os.path.join(FAULTS, name + ".yaml")


@functools.lru_cache(maxsize=64)
def replay(scenario, seed, fault_profile=None):
    from c3sim import runner
    cb, result = runner.run(ROOT, scenario, seed=seed,
                            faults=None if fault_profile is None else faults(fault_profile))
    return result, tuple(runner.check_result(cb, result))


def logs(events, name, node=None):
    return [e for e in events if e["kind"] == "log" and e.get("event") == name
            and (node is None or e.get("node") == node)]


def client_ends(events, action=None):
    return [e for e in events if e["kind"] == "client" and e.get("phase") == "end"
            and (action is None or e["action"] == action)]


def assert_clean(result, violations):
    assert list(violations) == [], list(violations)[:5]
    starts = [e for e in result.events if e["kind"] == "client" and e.get("phase") == "start"]
    assert len(client_ends(result.events)) == len(starts)
