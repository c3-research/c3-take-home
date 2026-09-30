"""Seed sweeps over every scenario and fault profile."""

import pytest
from rhelpers import assert_clean, client_ends, logs, replay

PROFILES = ["calm", "clocks", "crashes", "gpu", "network", "stragglers", "soak"]


@pytest.mark.parametrize("seed", range(101, 109))
def test_soak(seed):
    assert_clean(*replay("soak", seed))


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("seed", [7, 8])
def test_fault_profiles(profile, seed):
    assert_clean(*replay("public-template", seed, profile))


@pytest.mark.parametrize("scenario", ["base", "stragglers", "public-template"])
@pytest.mark.parametrize("seed", [21, 22])
def test_scenarios(scenario, seed):
    assert_clean(*replay(scenario, seed))


def test_calm_runs_complete_every_job():
    for seed in (1,):
        result, v = replay("base", seed, "calm")
        assert_clean(result, v)
        outs = {e["outcome"] for e in client_ends(result.events, "submit_job")}
        assert outs <= {"ok", "cancelled"}


def test_stragglers_race_to_commit():
    lost = 0
    for seed in range(1, 4):
        result, v = replay("stragglers", seed)
        assert_clean(result, v)
        lost += len(logs(result.events, "commit_lost"))
    assert lost > 0


def test_healthy_workers_survive_skew_and_pauses():
    for seed in range(1, 6):
        result, v = replay("soak", seed, "clocks")
        assert_clean(result, v)
        assert logs(result.events, "worker_dead") == []


def test_determinism_across_runs():
    a, _ = replay.__wrapped__("soak", 5)
    b, _ = replay.__wrapped__("soak", 5)
    assert a.sha256() == b.sha256()


def test_speculative_batch_for_a_job_that_finished_meanwhile():
    result, v = replay("soak", 536)
    assert_clean(result, v)
    assert logs(result.events, "unhandled_exception") == []
