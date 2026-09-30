"""End-to-end replays and the invariant checker."""

import copy

from harness import invariants_module, log_events, replay


def _clean(events, violations):
    assert violations == [], violations[:5]
    ends = [e for e in events if e["kind"] == "client" and e.get("phase") == "end"]
    starts = [e for e in events if e["kind"] == "client" and e.get("phase") == "start"]
    assert len(ends) == len(starts)


def test_base_replay_is_clean():
    events, v = replay("base", 1)
    _clean(events, v)
    ok = [e for e in events if e["kind"] == "client" and e.get("phase") == "end"
          and e["action"] == "submit_job" and e["outcome"] == "ok"]
    assert len(ok) >= 10


def test_soak_replay_is_clean():
    _clean(*replay("soak", 2))


def test_stragglers_get_speculated():
    events, v = replay("stragglers", 1)
    _clean(events, v)
    assert log_events(events, "speculation_launch")


def test_replay_is_deterministic():
    a, _ = replay("base", 3)
    b, _ = replay("base", 3)
    assert a == b


def test_checker_flags_a_second_commit():
    inv = invariants_module()
    events, _ = replay("base", 1)
    commits = log_events(events, "shard_committed")
    forged = copy.deepcopy(commits[0])
    forged["task"] = forged["task"] + "-other"
    forged["seq"] = 10 ** 9
    found = inv.check(events + [forged])
    assert any(v["invariant"] == "B1" for v in found)


def test_checker_flags_a_wrong_result():
    inv = invariants_module()
    events, _ = replay("base", 1)
    bad = copy.deepcopy(events)
    for e in bad:
        if (e["kind"] == "client" and e.get("phase") == "end" and e["action"] == "submit_job"
                and e["outcome"] == "ok"):
            e["result"]["result"]["sum"] += 1
            break
    assert any(v["invariant"] == "B3" for v in inv.check(bad))
