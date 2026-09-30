"""The commit log: first commit wins, per (job, attempt, shard), durably."""

from harness import FakeDisk

from shardmr.commit import CommitLog

OUT = {"shard": 2, "count": 3, "sum": 9, "digest": "x"}


def test_first_commit_wins():
    log = CommitLog(FakeDisk())
    won, rec, fresh = log.try_commit("A:j", 1, 2, "t1", "worker-1", OUT)
    assert (won, fresh, rec["task"]) == (True, True, "t1")
    won, rec, fresh = log.try_commit("A:j", 1, 2, "t2", "worker-2", dict(OUT, sum=10))
    assert (won, fresh, rec["task"]) == (False, False, "t1")
    assert log.get("A:j", 1, 2)["output"] == OUT


def test_repeated_commit_by_winner_is_not_fresh():
    log = CommitLog(FakeDisk())
    log.try_commit("A:j", 1, 2, "t1", "worker-1", OUT)
    won, _, fresh = log.try_commit("A:j", 1, 2, "t1", "worker-1", OUT)
    assert won and not fresh


def test_commits_survive_a_restart():
    disk = FakeDisk()
    CommitLog(disk).try_commit("A:j", 1, 2, "t1", "worker-1", OUT)
    again = CommitLog(disk)
    assert again.committed_shards("A:j", 1) == [2]
    won, _, _ = again.try_commit("A:j", 1, 2, "t9", "worker-3", OUT)
    assert not won


def test_attempts_are_separate():
    log = CommitLog(FakeDisk())
    log.try_commit("A:j", 1, 0, "t1", "w", dict(OUT, shard=0))
    log.try_commit("A:j", 2, 1, "t2", "w", dict(OUT, shard=1))
    assert log.committed_shards("A:j", 1) == [0]
    assert [i["attempt"] for i in log.inputs("A:j", 2)] == [2]


def test_inputs_are_in_shard_order():
    log = CommitLog(FakeDisk())
    for s in (3, 0, 2, 1):
        log.try_commit("A:j", 1, s, f"t{s}", "w", dict(OUT, shard=s))
    assert [i["shard"] for i in log.inputs("A:j", 1)] == [0, 1, 2, 3]
