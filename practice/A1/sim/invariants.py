"""Invariants for the job router (codebase A). See INVARIANTS.md.

check(events) -> [{"invariant", "t", "detail"}]

Common:  I0 (no effect without an op), I1 (capacity), I2 (client liveness), plus
         the GPU at-most-once sanity check. I3 is applied by the grader.
A1  no job has effects under two leases whose validity overlaps.
A2  every accepted job has exactly one SUCCEEDED GPU op, or ends CANCELLED with
    no effect after the cancel acknowledgement (or ends FAILED after
    MAX_GPU_ATTEMPTS failed ops).
A3  a lease holder never acts after its lease expires, allowing for the
    documented drift.

The checker reads only the trace. Lease validity is reconstructed from the
router's own records (grant, renew, restore, revoke, report) converted to true
time with the router's actual clock rate, which the trace records at boot.
"""

import hashlib
import json

from c3sim.check import (check_client_liveness, check_gpu_at_most_once, check_i0,
                         check_i1)
from c3sim.trace import scenario_record

# Documented in the service docs ("Operational notes"); duplicated here so the
# checker does not depend on service code.
MAX_GPU_ATTEMPTS = 8
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")
EPS = 1e-9


def V(inv, t, detail):
    return {"invariant": inv, "t": t, "detail": detail}


# --- trace reconstruction -------------------------------------------------------------

class Lease:
    __slots__ = ("key", "epoch", "holder", "boot", "router", "lease_s", "t_grant", "acks",
                 "restores", "t_revoke", "t_report", "effects")

    def __init__(self, e):
        self.key = e["key"]
        self.epoch = int(e["epoch"])
        self.holder = e["holder"]
        self.boot = e.get("holder_boot")
        self.router = e["node"]
        self.lease_s = float(e["lease_s"])
        self.t_grant = e["t"]
        self.acks = [e["t"]]
        self.restores = []
        self.t_revoke = None
        self.t_report = None
        self.effects = []


class Trace:
    def __init__(self, events):
        self.events = events
        self.header = scenario_record(events)
        self.rates = {}
        self.crashes = {}
        self.restarts = {}
        self.leases = {}
        self.by_job = {}
        for e in events:
            k = e["kind"]
            if k == "fault" and e.get("fault") == "clock_skew":
                self.rates[e["node"]] = 1.0 + float(e.get("drift_ppm", 0.0)) * 1e-6
            elif k == "crash":
                self.crashes.setdefault(e["node"], []).append(e["t"])
            elif k == "restart":
                self.restarts.setdefault(e["node"], []).append((e["t"], int(e["boot"])))
            elif k == "log":
                self._log(e)

    def _log(self, e):
        ev = e.get("event")
        if ev == "lease_granted":
            lease = Lease(e)
            self.leases[(lease.key, lease.epoch)] = lease
            self.by_job.setdefault(lease.key, []).append(lease)
            return
        if ev not in ("lease_renewed", "lease_restored", "lease_revoked", "report_applied"):
            return
        lease = self.leases.get((e.get("key"), e.get("epoch")))
        if lease is None:
            return
        if ev == "lease_renewed":
            lease.acks.append(e["t"])
        elif ev == "lease_restored":
            lease.restores.append(e["t"])
        elif ev == "lease_revoked":
            if lease.t_revoke is None:
                lease.t_revoke = e["t"]
        elif ev == "report_applied":
            if lease.t_report is None:
                lease.t_report = e["t"]

    def rate(self, node):
        return self.rates.get(node, 1.0)

    def boot_at(self, node, t):
        b = 0
        for tr, boot in self.restarts.get(node, []):
            if tr <= t:
                b = boot
        return b

    def crashed_between(self, node, t0, t1):
        return any(t0 <= tc <= t1 for tc in self.crashes.get(node, []))

    def true_len(self, lease):
        """True-time length of one acknowledgement of `lease` on its router's clock."""
        return lease.lease_s / self.rate(lease.router)

    def expiry_after(self, lease, t):
        """True-time expiry of `lease` as acknowledged to its holder up to time t."""
        acks = [a for a in lease.acks if a <= t + EPS]
        if not acks:
            return None
        return max(acks) + self.true_len(lease)

    def validity_end(self, lease):
        """End of the grantor-side validity interval (true time)."""
        end = max(lease.acks + lease.restores) + self.true_len(lease)
        if lease.t_report is not None:
            end = min(end, lease.t_report)
        if lease.t_revoke is not None and lease.boot is not None:
            if self.crashed_between(lease.holder, lease.t_grant, lease.t_revoke):
                end = min(end, lease.t_revoke)
        return end


def _holder_actions(events):
    """Lease-protected actions sent by holders: GPU submit/cancel and router reports.

    -> [(t, sender, key, epoch, what)]
    """
    out = []
    for e in events:
        if e["kind"] != "msg_send" or e.get("type") != "req":
            continue
        m, p = e["method"], e.get("payload") or {}
        if e["dst"] == "gpu" and m == "submit":
            inner = p.get("payload") or {}
            if "job" in inner and "epoch" in inner:
                out.append((e["t"], e["src"], inner["job"], inner["epoch"], "gpu_submit"))
        elif e["dst"] == "gpu" and m == "cancel":
            if "job" in p and "epoch" in p:
                out.append((e["t"], e["src"], p["job"], p["epoch"], "gpu_cancel"))
        elif m == "report" and "key" in p and "epoch" in p:
            out.append((e["t"], e["src"], p["key"], p["epoch"], "report"))
    return out


# --- A1 -------------------------------------------------------------------------------

def check_a1(tr, actions):
    out = []
    effects = [a for a in actions if a[4] != "report"]
    for lease in tr.leases.values():
        if lease.t_report is not None:
            effects.append((lease.t_report, lease.router, lease.key, lease.epoch,
                            "report_applied"))
    effects.sort(key=lambda a: a[0])
    for t, node, key, epoch, what in effects:
        own = tr.leases.get((key, epoch))
        if own is not None:
            own.effects.append(t)
        for other in tr.by_job.get(key, []):
            if other.epoch > epoch and other.t_grant <= t - EPS:
                out.append(V("A1", t, f"job {key}: {what} under epoch {epoch} at {t:.4f} "
                                      f"after epoch {other.epoch} was granted at "
                                      f"{other.t_grant:.4f}"))
                break
    for key in sorted(tr.by_job):
        leases = sorted((x for x in tr.by_job[key] if x.effects), key=lambda x: x.epoch)
        for a, b in zip(leases, leases[1:]):
            end = tr.validity_end(a)
            if b.t_grant < end - EPS:
                out.append(V("A1", b.t_grant, f"job {key}: epoch {b.epoch} granted at "
                                              f"{b.t_grant:.4f} while epoch {a.epoch} valid "
                                              f"until {end:.4f}; both had effects"))
    return out


# --- A3 -------------------------------------------------------------------------------

def check_a3(tr, actions):
    out = []
    for t, node, key, epoch, what in actions:
        lease = tr.leases.get((key, epoch))
        if lease is None:
            out.append(V("A3", t, f"{node}: {what} for job {key} under unknown epoch {epoch}"))
            continue
        if node != lease.holder:
            out.append(V("A3", t, f"{node}: {what} for job {key} epoch {epoch} held by "
                                  f"{lease.holder}"))
            continue
        if lease.boot is not None and tr.boot_at(node, t) != int(lease.boot):
            out.append(V("A3", t, f"{node}: {what} for job {key} epoch {epoch} from boot "
                                  f"{tr.boot_at(node, t)}, lease granted to boot {lease.boot}"))
            continue
        exp = tr.expiry_after(lease, t)
        if exp is None or t > exp + EPS:
            out.append(V("A3", t, f"{node}: {what} for job {key} epoch {epoch} at {t:.4f} "
                                  f"after lease expiry {exp if exp is None else round(exp, 4)}"))
    return out


# --- A2 -------------------------------------------------------------------------------

def _digest(op_id, payload):
    return hashlib.sha256((op_id + "\x00" + json.dumps(payload, sort_keys=True))
                          .encode()).hexdigest()[:16]


def check_a2(events):
    out = []
    accepted, terminal = {}, {}
    ops_of, op_payload, op_final, op_effects = {}, {}, {}, {}
    client_final = []
    for e in events:
        k = e["kind"]
        if k == "log":
            ev = e.get("event")
            if ev == "job_accepted":
                accepted.setdefault(e["key"], e["t"])
            elif ev == "job_terminal":
                terminal.setdefault(e["key"], e)
        elif k == "gpu_effect":
            op = e["op_id"]
            if e["effect"] == "accepted":
                job = (e.get("payload") or {}).get("job")
                if job is not None:
                    ops_of.setdefault(job, []).append(op)
                    op_payload[op] = e.get("payload") or {}
            elif e["effect"] == "finished":
                op_final[op] = e["state"]
            if e["effect"] in ("accepted", "started", "finished"):
                op_effects.setdefault(op, []).append((e["t"], e["effect"]))
        elif k == "client" and e.get("phase") == "end":
            if e.get("action") in ("submit_job", "cancel_job"):
                client_final.append(e)
    hdr = scenario_record(events)
    q = hdr.get("quiesce_at")
    for key in sorted(accepted):
        ops = ops_of.get(key, [])
        succ = [op for op in ops if op_final.get(op) == "SUCCEEDED"]
        term = terminal.get(key)
        if len(succ) > 1:
            out.append(V("A2", None, f"job {key}: {len(succ)} SUCCEEDED ops {sorted(succ)}"))
        if term is None:
            if q is None or accepted[key] <= q:
                out.append(V("A2", None, f"job {key}: accepted but never terminal"))
            continue
        st, t_term = term["state"], term["t"]
        if st == "SUCCEEDED":
            if len(succ) != 1:
                out.append(V("A2", t_term, f"job {key}: SUCCEEDED with {len(succ)} "
                                           f"SUCCEEDED ops"))
            elif term.get("op_id") != succ[0]:
                out.append(V("A2", t_term, f"job {key}: SUCCEEDED as {term.get('op_id')} "
                                           f"but {succ[0]} is the op that succeeded"))
        elif st == "CANCELLED":
            if succ:
                out.append(V("A2", t_term, f"job {key}: CANCELLED but op {succ[0]} SUCCEEDED"))
            for op in ops:
                late = [x for x in op_effects.get(op, []) if x[0] > t_term + EPS]
                if late:
                    out.append(V("A2", late[0][0], f"job {key}: op {op} {late[0][1]} at "
                                                   f"{late[0][0]:.4f} after cancel "
                                                   f"acknowledged at {t_term:.4f}"))
        elif st == "FAILED":
            failed = [op for op in ops if op_final.get(op) == "FAILED"]
            if succ or len(failed) < MAX_GPU_ATTEMPTS:
                out.append(V("A2", t_term, f"job {key}: FAILED after {len(failed)} failed ops "
                                           f"({len(succ)} succeeded)"))
    for e in client_final:
        res = e.get("result") or {}
        st = res.get("state")
        if st not in TERMINAL:
            continue
        key = f"{e.get('tenant', 'default')}/{e.get('job_id')}"
        term = terminal.get(key)
        if term is None or term["state"] != st:
            out.append(V("A2", e["t"], f"client told job {key} is {st}; router recorded "
                                       f"{term['state'] if term else 'no terminal state'}"))
            continue
        if st == "SUCCEEDED" and res.get("digest") is not None:
            op = term.get("op_id")
            if op in op_payload and res["digest"] != _digest(op, op_payload[op]):
                out.append(V("A2", e["t"], f"client got result digest {res['digest']} for "
                                           f"job {key}, not op {op}'s"))
    return out


def check(events):
    out = []
    out += check_i0(events)
    out += check_i1(events)
    out += check_gpu_at_most_once(events)
    out += check_client_liveness(events)
    tr = Trace(events)
    actions = _holder_actions(events)
    out += check_a1(tr, actions)
    out += check_a2(events)
    out += check_a3(tr, actions)
    return out
