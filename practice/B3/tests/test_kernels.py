"""Map/reduce kernels and identifiers."""

from harness import invariants_module

from shardmr import ids, kernels


def _map(jkey, attempt, shard, tid, records=16):
    return kernels.map_output(kernels.map_payload(jkey, attempt, shard, tid, records))


def test_map_output_depends_only_on_the_shard():
    a = _map("A:j", 1, 3, "task-a")
    b = _map("A:j", 2, 3, "task-b")
    assert a == b
    assert a != _map("A:j", 1, 4, "task-a")


def test_reduce_output_matches_reference():
    inv = invariants_module()
    inputs = [{"shard": s, "attempt": 1, "task": f"t{s}", "output": _map("A:j", 1, s, "t")}
              for s in reversed(range(5))]
    got = kernels.reduce_output(kernels.reduce_payload("A:j", 1, 5, inputs))
    assert got == inv.reference_result("A:j", 5, 16)


def test_op_ids_are_stable_and_distinct():
    tid = ids.task_id("A:j", 1, 3, 2, 17)
    assert ids.map_op_id(tid) == ids.map_op_id(ids.task_id("A:j", 1, 3, 2, 17))
    assert ids.map_op_id(tid) != ids.map_op_id(ids.task_id("A:j", 1, 3, 3, 17))
    assert ids.reduce_op_id("A:j", 1, 0) != ids.reduce_op_id("A:j", 2, 0)
    assert ids.job_key("A", "j") != ids.job_key("B", "j")
