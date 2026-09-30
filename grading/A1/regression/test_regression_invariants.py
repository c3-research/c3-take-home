"""The invariant checker flags hand-made violations in otherwise clean traces."""

import copy
import importlib.util
import os

import pytest

from helpers import ROOT, run, scenario

_inv_path = os.path.join(ROOT, "sim", "invariants.py")
if not os.path.isfile(_inv_path):
    _inv_path = os.path.join(ROOT, "invariants.py")
_spec = importlib.util.spec_from_file_location("a_invariants", _inv_path)
inv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inv)

WL = [{"repeat": 6, "at": 1.0, "every": 1.0, "action": "submit_job", "tenant": "A",
       "job_id": "j{i}", "units": 2, "duration": 1.0},
      {"at": 2.0, "action": "submit_job", "tenant": "C", "job_id": "c", "units": 1,
       "duration": 3.0},
      {"at": 3.0, "action": "cancel_job", "tenant": "C", "job_id": "c"}]


@pytest.fixture(scope="module")
def clean():
    events, violations, _ = run(scenario(WL))
    assert violations == []
    return events


def names(violations):
    return {v["invariant"] for v in violations}


def next_seq(events):
    return max(e["seq"] for e in events) + 1


def test_clean_trace_has_no_violations(clean):
    assert inv.check(clean) == []


def test_a3_flags_action_after_expiry(clean):
    ev = copy.deepcopy(clean)
    sub = next(e for e in ev if e["kind"] == "msg_send" and e["method"] == "submit"
               and e["dst"] == "gpu")
    late = dict(sub, t=sub["t"] + 30.0, seq=next_seq(ev), msg_id=10 ** 9)
    ev.append(late)
    ev.sort(key=lambda e: (e["t"], e["seq"]))
    assert "A3" in names(inv.check(ev))


def test_a3_flags_action_by_non_holder(clean):
    ev = copy.deepcopy(clean)
    sub = next(e for e in ev if e["kind"] == "msg_send" and e["method"] == "submit"
               and e["dst"] == "gpu")
    other = "worker-3" if sub["src"] != "worker-3" else "worker-2"
    ev.append(dict(sub, src=other, seq=next_seq(ev), msg_id=10 ** 9))
    ev.sort(key=lambda e: (e["t"], e["seq"]))
    assert "A3" in names(inv.check(ev))


def test_a1_flags_overlapping_grant(clean):
    ev = copy.deepcopy(clean)
    g = next(e for e in ev if e["kind"] == "log" and e.get("event") == "lease_granted")
    sub = next(e for e in ev if e["kind"] == "msg_send" and e["method"] == "submit"
               and e["dst"] == "gpu" and e["payload"]["payload"]["job"] == g["key"])
    t = sub["t"] - 1e-3
    ev.append(dict(g, t=t, epoch=g["epoch"] + 1, holder="worker-3", seq=next_seq(ev)))
    ev.sort(key=lambda e: (e["t"], e["seq"]))
    assert "A1" in names(inv.check(ev))


def test_a2_flags_second_succeeded_op(clean):
    ev = copy.deepcopy(clean)
    acc = next(e for e in ev if e["kind"] == "gpu_effect" and e["effect"] == "accepted"
               and e["op_id"].startswith("A/"))
    fin = next(e for e in ev if e["kind"] == "gpu_effect" and e["effect"] == "finished"
               and e["op_id"] == acc["op_id"])
    s = next_seq(ev)
    op2 = acc["op_id"][:-1] + "9"
    ev.append(dict(acc, op_id=op2, seq=s))
    ev.append(dict(fin, op_id=op2, seq=s + 1))
    ev.sort(key=lambda e: (e["t"], e["seq"]))
    assert "A2" in names(inv.check(ev))


def test_a2_flags_effect_after_cancel(clean):
    ev = copy.deepcopy(clean)
    term = next((e for e in ev if e["kind"] == "log" and e.get("event") == "job_terminal"
                 and e["state"] == "CANCELLED"), None)
    if term is None:
        pytest.skip("cancel lost the race on this seed")
    op = "C/c/a1"
    acc = next((e for e in ev if e["kind"] == "gpu_effect" and e["op_id"] == op
                and e["effect"] == "accepted"), None)
    if acc is None:
        base = next(e for e in ev if e["kind"] == "gpu_effect" and e["effect"] == "accepted")
        acc = dict(base, op_id=op, payload={"job": "C/c", "attempt": 1, "epoch": 1},
                   t=term["t"] - 0.5, seq=next_seq(ev))
        ev.append(acc)
    ev.append(dict(acc, effect="started", t=term["t"] + 1.0, seq=next_seq(ev)))
    ev.sort(key=lambda e: (e["t"], e["seq"]))
    assert "A2" in names(inv.check(ev))


def test_a2_flags_client_told_wrong_state(clean):
    ev = copy.deepcopy(clean)
    end = next(e for e in ev if e["kind"] == "client" and e.get("phase") == "end"
               and e["action"] == "submit_job" and e.get("tenant") == "A")
    end["result"] = dict(end["result"], state="FAILED")
    assert "A2" in names(inv.check(ev))


def test_i1_flags_over_capacity(clean):
    ev = copy.deepcopy(clean)
    eff = next(e for e in ev if e["kind"] == "gpu_effect")
    eff["committed"] = eff["capacity"] + 1
    assert "I1" in names(inv.check(ev))


def test_i2_flags_unresolved_action(clean):
    ev = [e for e in copy.deepcopy(clean)
          if not (e["kind"] == "client" and e.get("phase") == "end" and e["action_id"] == 0)]
    assert "I2" in names(inv.check(ev))
