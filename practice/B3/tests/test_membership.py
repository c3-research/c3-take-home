"""Worker registration, epochs and failure detection."""

import pytest
from c3sim import RpcError
from harness import FakeCtx

from shardmr import config
from shardmr.membership import Membership
from shardmr.store import JobStore


class _Node:
    def __init__(self, ctx):
        self.clock = ctx.clock
        self.log = ctx.log


def _members(disk=None):
    ctx = FakeCtx("coord", disk=disk)
    return Membership(_Node(ctx), JobStore(ctx.disk)), ctx


def test_epochs_increase_and_persist():
    m, ctx = _members()
    e1, fresh = m.register("worker-1", 0, 1)
    assert fresh
    e2, _ = m.register("worker-1", 1, 1)
    assert e2 > e1
    m2, _ = _members(ctx.disk)
    e3, _ = m2.register("worker-1", 2, 1)
    assert e3 > e2


def test_repeated_registration_is_idempotent():
    m, _ = _members()
    e1, _ = m.register("worker-1", 0, 1)
    e2, fresh = m.register("worker-1", 0, 1)
    assert (e2, fresh) == (e1, False)


def test_stale_registration_rejected():
    m, _ = _members()
    m.register("worker-1", 1, 1)
    with pytest.raises(RpcError) as ei:
        m.register("worker-1", 0, 5)
    assert ei.value.code == "STALE_REGISTER"


def test_dead_after_budget():
    assert config.worker_dead_after() == pytest.approx(
        config.WORKER_TIMEOUT_S * (1 + config.MAX_DRIFT) + config.PAUSE_ALLOWANCE_S)


def test_silent_worker_is_fenced():
    m, ctx = _members()
    epoch, _ = m.register("worker-1", 0, 1)
    ctx.clock.t = config.worker_dead_after() - 0.01
    assert m.expire() == []
    ctx.clock.t = config.worker_dead_after() + 0.01
    assert m.expire() == ["worker-1"]
    assert not m.is_current("worker-1", epoch)
    info, _ = m.heartbeat("worker-1", epoch, 5)
    assert info is None
    again, _ = m.register("worker-1", 0, 2)
    assert again > epoch + 1


def test_old_heartbeat_is_not_fresh():
    m, _ = _members()
    epoch, _ = m.register("worker-1", 0, 1)
    assert m.heartbeat("worker-1", epoch, 3)[1]
    assert not m.heartbeat("worker-1", epoch, 2)[1]
