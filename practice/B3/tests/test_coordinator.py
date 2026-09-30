"""Coordinator handlers driven directly (no simulator)."""

import pytest
from c3sim import RpcError
from harness import call, make_coordinator

from shardmr import kernels

JOB = {"tenant": "A", "job_id": "j1", "shards": 3, "records": 8, "units": 1, "duration": 1.0}


def _setup(workers=("worker-1",)):
    c, ctx = make_coordinator()
    epochs = {w: call(c, "register", {"worker": w, "boot": 0, "reg_seq": 1})["epoch"]
              for w in workers}
    call(c, "submit_job", dict(JOB))
    c.sched.tick()
    ctx.close_spawned()
    return c, ctx, epochs


def _tasks(c, worker=None):
    return sorted((t for t in c.sched.tasks.values() if worker in (None, t.worker)),
                  key=lambda t: t.shard)


def _commit(c, t, epoch, attempt=None):
    out = kernels.map_output(kernels.map_payload(t.jkey, t.attempt, t.shard, t.tid, 8))
    return call(c, "commit", {"worker": t.worker, "epoch": epoch, "task": t.tid, "job": t.jkey,
                              "attempt": t.attempt if attempt is None else attempt,
                              "shard": t.shard, "output": out}, src=t.worker)


def test_submit_is_idempotent_per_tenant_and_job():
    c, _ = make_coordinator()
    r1 = call(c, "submit_job", dict(JOB))
    r2 = call(c, "submit_job", dict(JOB))
    assert r1["accepted"] and r2["accepted"] and r2["attempt"] == r1["attempt"] == 1
    with pytest.raises(RpcError) as ei:
        call(c, "submit_job", dict(JOB, shards=4))
    assert ei.value.code == "CONFLICT"
    assert call(c, "submit_job", dict(JOB, tenant="B"))["attempt"] == 1


def test_submit_validates_spec():
    c, _ = make_coordinator()
    with pytest.raises(RpcError) as ei:
        call(c, "submit_job", dict(JOB, shards=0))
    assert ei.value.code == "INVALID"


def test_cancel_before_submit_is_remembered():
    c, _ = make_coordinator()
    assert call(c, "cancel_job", {"tenant": "A", "job_id": "j1"})["state"] == "CANCELLED"
    assert call(c, "submit_job", dict(JOB))["state"] == "CANCELLED"
    assert call(c, "job_status", {"tenant": "A", "job_id": "j1"})["state"] == "CANCELLED"


def test_batch_dispatch_respects_slots():
    c, _, _ = _setup()
    assert len(_tasks(c)) == 3
    assert {t.batch for t in _tasks(c)} and len({t.batch for t in _tasks(c)}) == 1


def test_first_commit_wins_between_duplicates():
    c, ctx, ep = _setup(("worker-1", "worker-2"))
    orig = _tasks(c)[0]
    jp = c.sched.jobs[orig.jkey]
    c.sched._dispatch_batch(c.members.workers["worker-2"], [(jp, orig.shard)], speculative=True)
    ctx.close_spawned()
    spec = [t for t in _tasks(c, "worker-2") if t.shard == orig.shard and t.speculative][0]
    assert _commit(c, spec, ep["worker-2"])["committed"]
    r = _commit(c, orig, ep[orig.worker])
    assert r == {"committed": False, "winner": spec.tid}
    hb = call(c, "heartbeat", {"worker": orig.worker, "epoch": ep[orig.worker], "hb_seq": 1,
                               "running": [orig.tid]})
    assert orig.tid in hb["abort"]


def test_commit_from_fenced_epoch_rejected():
    c, _, ep = _setup()
    t = _tasks(c)[0]
    c.members.fence("worker-1", "test")
    with pytest.raises(RpcError) as ei:
        _commit(c, t, ep["worker-1"])
    assert ei.value.code == "FENCED"


def test_commit_for_other_attempt_rejected():
    c, _, ep = _setup()
    t = _tasks(c)[0]
    with pytest.raises(RpcError) as ei:
        _commit(c, t, ep["worker-1"], attempt=t.attempt + 1)
    assert ei.value.code == "STALE_ATTEMPT"


def test_partial_batch_failure_requeues_only_failed_shards():
    c, _, ep = _setup()
    ts = _tasks(c)
    for t in (ts[0], ts[2]):
        assert _commit(c, t, ep["worker-1"])["committed"]
    call(c, "report", {"worker": "worker-1", "epoch": ep["worker-1"], "batch": ts[0].batch,
                       "results": [{"task": ts[0].tid, "outcome": "committed"},
                                   {"task": ts[1].tid, "outcome": "failed"},
                                   {"task": ts[2].tid, "outcome": "committed"}]})
    assert [q[2] for q in c.sched.queue] == [ts[1].shard]
    assert c.sched.jobs[ts[0].jkey].shards[ts[1].shard].failures == 1


def test_all_shards_committed_starts_reduce():
    c, ctx, ep = _setup()
    for t in _tasks(c):
        _commit(c, t, ep["worker-1"])
    st = call(c, "job_status", {"tenant": "A", "job_id": "j1"})
    assert st["state"] == "REDUCING" and st["committed"] == 3
    ctx.close_spawned()
