"""Targeted protocol scenarios: GPU quirks, crashes, pauses, cancels, releases."""

from helpers import logs, outcomes, run, scenario

A_JOBS = [{"repeat": 12, "at": 1.0, "every": 1.0, "action": "submit_job", "tenant": "A",
           "job_id": "j{i}", "units": 2, "duration": 1.0}]


def gpu_effects(events, effect=None):
    return [e for e in events if e["kind"] == "gpu_effect"
            and (effect is None or e["effect"] == effect)]


def test_unavailable_after_accept_polls_before_resubmit():
    faults = {"gpu": {"accepted_reported_failed": 1.0,
                      "latency": {"dist": "uniform", "low": 0.2, "high": 0.8}}}
    events, violations, summary = run(scenario(A_JOBS, faults))
    assert violations == []
    assert all(a["outcome"] == "ok" for a in summary["actions"])
    accepted = gpu_effects(events, "accepted")
    assert sorted(e["op_id"] for e in accepted) == sorted({e["op_id"] for e in accepted})
    assert all(e["op_id"].endswith("/a1") for e in accepted)
    assert logs(events, "gpu_submit_unknown")


def test_lost_submit_replies_do_not_duplicate_work():
    faults = {"gpu": {"lost_reply": 1.0}}
    events, violations, summary = run(scenario(A_JOBS, faults))
    assert violations == []
    assert len(gpu_effects(events, "started")) == 12


def test_failed_ops_retry_with_new_op_id_then_fail_job():
    from jobrouter import config
    faults = {"gpu": {"fail": 1.0, "latency": {"dist": "constant", "value": 0.1}}}
    wl = [{"at": 1.0, "action": "submit_job", "tenant": "A", "job_id": "f", "units": 1,
           "duration": 0.1}]
    events, violations, summary = run(scenario(wl, faults))
    assert violations == []
    assert summary["actions"][0]["outcome"] == "failed"
    ops = [e["op_id"] for e in gpu_effects(events, "accepted")]
    assert len(ops) == config.MAX_GPU_ATTEMPTS == len(set(ops))
    assert len(gpu_effects(events, "release_complete")) == config.MAX_GPU_ATTEMPTS


def test_worker_crash_revokes_and_regrants():
    faults = {"nodes": {"crashes": [{"node": "worker-1", "at": 3.3, "restart_after": 1.0},
                                    {"node": "worker-2", "at": 5.6, "restart_after": 2.0}]}}
    events, violations, summary = run(scenario(A_JOBS, faults))
    assert violations == []
    assert all(a["outcome"] == "ok" for a in summary["actions"])


def test_router_crash_restores_leases_and_releases():
    faults = {"nodes": {"crashes": [{"node": "router-1", "at": 4.1, "restart_after": 1.5},
                                    {"node": "router-1", "at": 9.7, "restart_after": 0.5}]}}
    events, violations, summary = run(scenario(A_JOBS, faults))
    assert violations == []
    assert all(a["outcome"] == "ok" for a in summary["actions"])
    assert len(logs(events, "router_start")) == 3


def test_paused_holder_is_fenced_not_double_run():
    faults = {"nodes": {"pauses": [{"node": "worker-1", "at": 2.2, "duration": 4.0},
                                   {"node": "worker-2", "at": 3.1, "duration": 3.5}]}}
    events, violations, summary = run(scenario(A_JOBS, faults, capacity=12))
    assert violations == []
    for op in {e["op_id"] for e in gpu_effects(events, "accepted")}:
        assert len([e for e in gpu_effects(events, "started") if e["op_id"] == op]) == 1


def test_clock_drift_at_documented_bound():
    faults = {"nodes": {"clocks": [{"node": "worker-*", "drift_ppm": [-1000, 1000],
                                    "offset": [-0.5, 0.5]},
                                   {"node": "router-1", "drift_ppm": 1000, "offset": -0.5}],
                        "pauses": [{"node": "worker-*", "rate_per_hour": 300,
                                    "duration": [0.2, 2.5]}]}}
    for seed in (1, 2, 3):
        _, violations, _ = run(scenario(A_JOBS, faults), seed=seed)
        assert violations == [], (seed, violations[:3])


def test_cancel_races_completion_consistently():
    wl = []
    for i in range(12):
        wl.append({"at": 1.0 + i, "action": "submit_job", "tenant": "C", "job_id": f"c{i}",
                   "units": 1, "duration": 1.0})
        wl.append({"at": 1.0 + i + 0.15 * i, "action": "cancel_job", "tenant": "C",
                   "job_id": f"c{i}"})
    faults = {"network": {"drop": 0.02, "duplicate": 0.02},
              "gpu": {"latency": {"dist": "uniform", "low": 0.1, "high": 1.0}}}
    for seed in (1, 2, 3, 4):
        events, violations, summary = run(scenario(wl, faults), seed=seed)
        assert violations == [], (seed, violations[:3])
        states = {e["key"]: e["state"] for e in logs(events, "job_terminal")}
        assert set(states.values()) <= {"SUCCEEDED", "CANCELLED"}


def test_precancel_then_submit_never_runs():
    wl = [{"at": 1.0, "action": "cancel_job", "tenant": "A", "job_id": "p"},
          {"at": 2.0, "action": "submit_job", "tenant": "A", "job_id": "p", "units": 1,
           "duration": 1.0}]
    events, violations, summary = run(scenario(wl))
    assert violations == []
    assert outcomes(summary)[1]["outcome"] == "cancelled"
    assert gpu_effects(events) == []


def test_mismatched_resubmit_rejected():
    wl = [{"at": 1.0, "action": "submit_job", "tenant": "A", "job_id": "m", "units": 1,
           "duration": 1.0},
          {"at": 1.5, "action": "submit_job", "tenant": "A", "job_id": "m", "units": 2,
           "duration": 1.0}]
    _, violations, summary = run(scenario(wl))
    assert outcomes(summary)[1]["outcome"] == "error"
    assert outcomes(summary)[1]["code"] == "MISMATCH"


def test_capacity_waits_for_release_complete():
    wl = [{"repeat": 10, "at": 1.0, "every": 0.3, "action": "submit_job", "tenant": "A",
           "job_id": "r{i}", "units": 3, "duration": 0.5}]
    sc = scenario(wl, {"network": {"drop": 0.05}}, capacity=6)
    sc["gpu"]["release_delay"] = {"dist": "uniform", "low": 1.0, "high": 2.0}
    events, violations, summary = run(sc)
    assert violations == []
    assert max(e["committed"] for e in gpu_effects(events)) <= 6
    assert not logs(events, "gpu_capacity_wait")
    freed = logs(events, "capacity_freed")
    assert len(freed) == 10 and freed[-1]["reserved"] == 0


def test_large_jobs_are_not_starved():
    wl = [{"repeat": 40, "at": 1.0, "every": 0.4, "action": "submit_job", "tenant": "A",
           "job_id": "s{i}", "units": 2, "duration": 1.0},
          {"at": 2.0, "action": "submit_job", "tenant": "B", "job_id": "big", "units": 8,
           "duration": 1.0}]
    events, violations, summary = run(scenario(wl, capacity=8))
    assert violations == []
    big = outcomes(summary)[1]
    assert big["outcome"] == "ok" and big["t_end"] < 20.0


def test_stats_reports_counters():
    wl = A_JOBS[:1] + [{"at": 30.0, "action": "stats"}]
    _, violations, summary = run(scenario(wl))
    st = [a for a in summary["actions"] if a["action"] == "stats"][0]
    assert st["outcome"] == "ok"
    assert st["result"]["counts"]["submitted"] == 12
    assert st["result"]["reserved"] == 0
