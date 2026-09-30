from jobrouter import models
from jobrouter.capacity import CapacityLedger
from jobrouter.dispatch import Dispatcher
from jobrouter.store import JobStore

from helpers import FakeNode


def make(node=None, capacity=8):
    node = node or FakeNode()
    store = JobStore(node)
    store.load()
    ledger = CapacityLedger(node, capacity)
    return node, store, ledger


def add(store, tenant, job_id, units):
    job = models.new_job(tenant, job_id, units, 1.0, store.next_seq())
    store.put(job)
    return job


def test_store_persists_and_reloads_queue_order():
    node, store, _ = make()
    add(store, "A", "x", 1)
    add(store, "A", "y", 1)
    add(store, "B", "z", 1)
    store2 = JobStore(node)
    assert store2.load() == 3
    assert [j["job_id"] for j in store2.queued()] == ["x", "y", "z"]
    assert store2.next_seq() == 4


def test_store_dequeues_on_state_change():
    _, store, _ = make()
    job = add(store, "A", "x", 1)
    job["state"] = models.LEASED
    store.put(job)
    assert store.queue_depth() == 0


def test_ledger_reserve_and_free():
    node, store, ledger = make(capacity=4)
    job = add(store, "A", "x", 3)
    ledger.reserve(job, "A/x/a1")
    assert ledger.reserved() == 3 and not ledger.fits(2)
    assert ledger.mark_freed(job, "A/x/a1")
    assert ledger.reserved() == 0
    assert not ledger.mark_freed(job, "A/x/a1")


def test_ledger_rebuild_counts_releasing_ops():
    node, store, ledger = make(capacity=8)
    job = add(store, "A", "x", 2)
    ledger.reserve(job, "A/x/a1")
    job["ops"]["A/x/a1"]["rel"] = models.REL_RELEASING
    store.put(job)
    ledger2 = CapacityLedger(node, 8)
    assert ledger2.rebuild(store.ordered()) == 2


def test_dispatch_in_submission_order():
    node, store, ledger = make(capacity=8)
    add(store, "A", "x", 2)
    add(store, "A", "y", 2)
    d = Dispatcher(node, store, ledger)
    assert d.next_job()["job_id"] == "x"


def test_dispatch_holds_capacity_for_blocked_head():
    node, store, ledger = make(capacity=4)
    busy = add(store, "A", "busy", 2)
    ledger.reserve(busy, "A/busy/a1")
    busy["state"] = models.LEASED
    store.put(busy)
    add(store, "B", "big", 3)
    add(store, "A", "small", 2)
    d = Dispatcher(node, store, ledger)
    assert d.next_job() is None
    assert node.events("dispatch_blocked")[0]["key"] == "B/big"
