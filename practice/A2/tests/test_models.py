import pytest
from c3sim import RpcError

from jobrouter import models


def test_job_key_and_op_ids_are_deterministic():
    assert models.job_key("A", "j1") == "A/j1"
    assert models.op_id_for("A/j1", 1) == "A/j1/a1"
    job = models.new_job("A", "j1", 2, 1.0, 1)
    assert models.current_op_id(job) == "A/j1/a1"
    job["attempt"] = 3
    assert models.current_op_id(job) == "A/j1/a3"


def test_new_job_defaults():
    job = models.new_job("A", "j1", 2, 1.5, 7)
    assert job["state"] == models.QUEUED
    assert job["epoch"] == 0 and job["attempt"] == 1
    assert job["ops"] == {} and not job["cancel"]
    assert not models.is_terminal(job)


def test_tombstone_is_cancelled_and_precancelled():
    job = models.tombstone("A", "j9", 3)
    assert job["state"] == models.CANCELLED and job["precancelled"]
    assert models.is_terminal(job)


def test_parse_submit_validates():
    assert models.parse_submit({"tenant": "A", "job_id": "x", "units": 2, "duration": 1}) == \
        ("A", "x", 2, 1.0)
    for bad in ({"tenant": "", "job_id": "x", "units": 1},
                {"tenant": "A", "job_id": "x", "units": 0},
                {"tenant": "A", "job_id": "x", "units": True},
                {"tenant": "A", "job_id": "x", "units": 1, "duration": -1}):
        with pytest.raises(RpcError) as e:
            models.parse_submit(bad)
        assert e.value.code == "INVALID"


def test_parse_lease_ref():
    assert models.parse_lease_ref({"key": "A/x", "epoch": 2, "boot": 1}) == ("A/x", 2, 1)
    with pytest.raises(RpcError):
        models.parse_lease_ref({"key": "A/x", "epoch": 0})


def test_public_view_includes_result_only_when_succeeded():
    job = models.new_job("A", "j1", 2, 1.0, 1)
    assert "result" not in models.public_view(job)
    job.update(state=models.SUCCEEDED, result={"digest": "d"}, final_op="A/j1/a1")
    view = models.public_view(job)
    assert view["result"] == {"digest": "d"} and view["op_id"] == "A/j1/a1"


def test_same_submission():
    job = models.new_job("A", "j1", 2, 1.0, 1)
    assert models.same_submission(job, 2, 1.0)
    assert not models.same_submission(job, 3, 1.0)
