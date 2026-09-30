"""Protocol properties checked on traces of faulty runs."""

from rhelpers import assert_clean, logs, replay


def _gpu_calls(events):
    """Per (node, op_id): ordered list of (method, reply-kind) for GPU RPCs."""
    reqs, seq = {}, {}
    for e in events:
        if e["kind"] != "msg_send":
            continue
        if e.get("dst") == "gpu" and e.get("type") == "req":
            op = (e.get("payload") or {}).get("op_id")
            reqs[e["msg_id"]] = (e["src"], op, e["method"])
            seq.setdefault((e["src"], op), []).append([e["method"], None])
        elif e.get("src") == "gpu" and e.get("type") == "resp" and e.get("rpc_id") in reqs:
            node, op, method = reqs[e["rpc_id"]]
            err = (e.get("payload") or {}).get("err")
            for item in reversed(seq[(node, op)]):
                if item[0] == method and item[1] is None:
                    item[1] = err[0] if err else "ok"
                    break
    return seq


def test_unavailable_submit_is_resolved_with_status():
    checked = 0
    for seed in range(1, 9):
        result, v = replay("soak", seed, "gpu")
        assert_clean(result, v)
        for calls in _gpu_calls(result.events).values():
            for i, (method, reply) in enumerate(calls):
                if method == "submit" and reply == "UNAVAILABLE" and i + 1 < len(calls):
                    checked += 1
                    assert calls[i + 1][0] == "status"
    assert checked > 0


def test_every_accepted_op_is_released():
    for seed in range(1, 4):
        result, v = replay("soak", seed)
        assert_clean(result, v)
        accepted, freed = set(), set()
        for e in result.events:
            if e["kind"] == "gpu_effect":
                if e["effect"] == "accepted":
                    accepted.add(e["op_id"])
                elif e["effect"] == "release_complete":
                    freed.add(e["op_id"])
        assert accepted - freed == set()


def test_worker_epochs_strictly_increase():
    for seed in range(1, 4):
        result, v = replay("soak", seed, "crashes")
        assert_clean(result, v)
        last = {}
        for e in logs(result.events, "worker_registered", node="coord"):
            assert e["epoch"] > last.get(e["worker"], 0)
            last[e["worker"]] = e["epoch"]
        for e in logs(result.events, "worker_dead", node="coord"):
            assert e["epoch"] > last.get(e["worker"], 0)
            last[e["worker"]] = e["epoch"]


def test_one_gpu_op_per_map_task_across_restarts():
    for seed in range(1, 4):
        result, v = replay("soak", seed, "crashes")
        assert_clean(result, v)
        per_task = {}
        for e in result.events:
            if (e["kind"] == "gpu_effect" and e["effect"] == "accepted"
                    and (e.get("payload") or {}).get("kind") == "map"):
                per_task.setdefault(e["payload"]["task"], set()).add(e["op_id"])
        assert all(len(ops) == 1 for ops in per_task.values())


def test_reduce_reads_one_attempt():
    for seed in range(1, 4):
        result, v = replay("soak", seed)
        assert_clean(result, v)
        for e in result.events:
            if (e["kind"] == "gpu_effect" and e["effect"] == "accepted"
                    and (e.get("payload") or {}).get("kind") == "reduce"):
                p = e["payload"]
                assert {i["attempt"] for i in p["inputs"]} == {p["attempt"]}
                assert [i["shard"] for i in p["inputs"]] == list(range(p["shards"]))


def test_abandoned_attempts_restart_every_shard():
    seen = 0
    for seed in range(1, 9):
        result, v = replay("soak", seed, "gpu")
        assert_clean(result, v)
        for e in logs(result.events, "attempt_abandoned"):
            seen += 1
            nxt = [s for s in logs(result.events, "job_scheduled")
                   if s["job"] == e["job"] and s["attempt"] == e["attempt"] + 1]
            if nxt:
                assert nxt[0]["queued"] == nxt[0]["shards"]
    assert seen > 0


def test_coordinator_restart_resumes_jobs():
    resumed = 0
    for seed in range(1, 4):
        result, v = replay("soak", seed)
        assert_clean(result, v)
        boots = logs(result.events, "coord_recovered")
        resumed += sum(1 for b in boots if b.get("mapping", 0) or b.get("reducing", 0))
    assert resumed > 0
