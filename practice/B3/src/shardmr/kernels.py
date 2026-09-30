"""Models of the map and reduce kernels.

The simulated GPU runs an operation and returns its payload as the result. The
functions here turn that returned payload into the kernel's output, exactly as
the real kernels would compute it from the shard's input split. They are pure
and deterministic: the same shard always produces the same output, whichever
task or attempt ran it.
"""

import hashlib


def _h(text):
    return hashlib.sha256(text.encode()).hexdigest()


def shard_records(jkey, shard, n):
    """The input split for one shard: `n` integer records."""
    return [int(_h(f"{jkey}#{int(shard)}#{k}")[:8], 16) % 10007 for k in range(int(n))]


def map_payload(jkey, attempt, shard, tid, records):
    """GPU payload of a map operation (the kernel's input descriptor)."""
    return {"kind": "map", "job": jkey, "attempt": int(attempt), "shard": int(shard),
            "task": tid, "records": int(records)}


def map_output(result_payload):
    """Output of the map kernel for the shard described by `result_payload`."""
    recs = shard_records(result_payload["job"], result_payload["shard"],
                         result_payload["records"])
    return {"shard": int(result_payload["shard"]), "count": len(recs), "sum": sum(recs),
            "digest": _h(",".join(str(r) for r in recs))[:16]}


def reduce_payload(jkey, attempt, shards, inputs):
    """GPU payload of a reduce operation. `inputs` are committed shard outputs."""
    return {"kind": "reduce", "job": jkey, "attempt": int(attempt), "shards": int(shards),
            "inputs": inputs}


def reduce_output(result_payload):
    """Final job result from the reduce operation's payload."""
    outs = sorted((i["output"] for i in result_payload["inputs"]), key=lambda o: o["shard"])
    return {"shards": len(outs), "count": sum(o["count"] for o in outs),
            "sum": sum(o["sum"] for o in outs),
            "checksum": _h("|".join(f"{o['shard']}:{o['digest']}" for o in outs))[:16]}
