from jobrouter import config
from jobrouter.leases import HolderLease, LeaseTable

from helpers import FakeClock


def grant(epoch=1, lease_s=config.LEASE_S):
    return {"key": "A/j1", "tenant": "A", "job_id": "j1", "units": 2, "duration": 1.0,
            "attempt": 1, "epoch": epoch, "op_id": "A/j1/a1", "mode": "run",
            "lease_s": lease_s}


def test_holder_budget_is_shorter_than_lease():
    assert config.holder_budget() < config.LEASE_S
    assert config.regrant_after() > config.LEASE_S


def test_holder_lease_measured_from_send_time():
    clock = FakeClock(10.0)
    lease = HolderLease(clock, grant(), sent_at=9.5)
    assert abs(lease.valid_until - (9.5 + config.holder_budget())) < 1e-12
    assert lease.valid()
    clock.t = lease.valid_until
    assert not lease.valid()


def test_extend_never_shortens():
    clock = FakeClock(0.0)
    lease = HolderLease(clock, grant(), sent_at=0.0)
    before = lease.valid_until
    lease.extend(-1.0)
    assert lease.valid_until == before
    lease.extend(0.5)
    assert lease.valid_until == 0.5 + config.holder_budget()


def test_lost_lease_is_invalid():
    lease = HolderLease(FakeClock(0.0), grant(), sent_at=0.0)
    lease.lose("fenced")
    assert not lease.valid() and lease.lost == "fenced"


def test_lease_table_regrant_timing():
    clock = FakeClock(0.0)
    table = LeaseTable(clock)
    lease = table.grant("A/j1", 1, "worker-1", 0)
    assert table.is_live(lease)
    clock.t = config.LEASE_S + 0.01
    assert not table.is_live(lease)
    assert table.due_for_regrant() == []
    clock.t = config.regrant_after() + 0.01
    assert [x.key for x in table.due_for_regrant()] == ["A/j1"]


def test_renew_moves_expiry():
    clock = FakeClock(0.0)
    table = LeaseTable(clock)
    lease = table.grant("A/j1", 1, "worker-1", 0)
    clock.t = 1.0
    table.renew(lease)
    assert lease.expires_at() == 1.0 + config.LEASE_S
    assert [x.key for x in table.held_by("worker-1")] == ["A/j1"]
