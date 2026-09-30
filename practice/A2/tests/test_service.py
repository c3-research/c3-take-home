"""End-to-end smoke tests on the simulator, without faults."""

from helpers import logs, outcomes, run, scenario


def test_fault_free_jobs_all_succeed():
    wl = [{"repeat": 10, "at": 1.0, "every": 1.0, "action": "submit_job", "tenant": "A",
           "job_id": "j{i}", "units": 2, "duration": 1.0}]
    events, violations, summary = run(scenario(wl))
    assert violations == []
    assert all(a["outcome"] == "ok" for a in summary["actions"])
    assert len(logs(events, "job_terminal")) == 10


def test_duplicate_submit_runs_once():
    wl = [{"at": 1.0, "action": "submit_job", "tenant": "A", "job_id": "same", "units": 1,
           "duration": 1.0},
          {"at": 1.5, "action": "submit_job", "tenant": "A", "job_id": "same", "units": 1,
           "duration": 1.0}]
    events, violations, summary = run(scenario(wl))
    assert violations == []
    started = [e for e in events if e["kind"] == "gpu_effect" and e["effect"] == "started"]
    assert len(started) == 1
    assert len(logs(events, "submit_duplicate")) >= 1


def test_cancel_before_dispatch_is_immediate():
    wl = [{"at": 1.0, "action": "submit_job", "tenant": "A", "job_id": "big", "units": 12,
           "duration": 5.0},
          {"at": 1.2, "action": "submit_job", "tenant": "A", "job_id": "c", "units": 1,
           "duration": 1.0},
          {"at": 1.4, "action": "cancel_job", "tenant": "A", "job_id": "c"}]
    events, violations, summary = run(scenario(wl))
    assert violations == []
    acts = outcomes(summary)
    assert acts[1]["outcome"] == "cancelled"
    assert not [e for e in events if e["kind"] == "gpu_effect" and e["op_id"].startswith("A/c/")]


def test_units_over_capacity_rejected():
    wl = [{"at": 1.0, "action": "submit_job", "tenant": "A", "job_id": "huge", "units": 13,
           "duration": 1.0}]
    _, violations, summary = run(scenario(wl))
    assert summary["actions"][0]["outcome"] == "error"
    assert summary["actions"][0]["code"] == "INVALID"


def test_base_scenario_is_clean():
    _, violations, summary = run("base", seed=1)
    assert violations == []
