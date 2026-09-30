"""Run results: trace.jsonl, logs/<node>.log and summary.json (runtime.md sections 7 and 9)."""

import hashlib
import json
import os


def dumps_event(ev):
    return json.dumps(ev, sort_keys=True, separators=(",", ":"))


def trace_text(events):
    return "".join(dumps_event(e) + "\n" for e in events)


def read_trace(path):
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def scenario_record(events):
    """The run's scenario header (first `log` record from node `sim`), or {}."""
    for e in events:
        if e.get("kind") == "log" and e.get("node") == "sim" and e.get("event") == "scenario":
            return e
        if e.get("seq", 0) > 5:
            break
    return {}


class RunResult:
    def __init__(self, sim, error=None):
        self.sim = sim
        self.events = sim.events
        self.error = error
        self._text = None

    @property
    def text(self):
        if self._text is None:
            self._text = trace_text(self.events)
        return self._text

    def sha256(self):
        return hashlib.sha256(self.text.encode()).hexdigest()

    @property
    def actions(self):
        c = self.sim.client_node
        if c is None:
            return []
        return [c.records[k] for k in sorted(c.records)]

    def summary(self):
        sim = self.sim
        acts = self.actions
        outcomes = {}
        for a in acts:
            outcomes[a["outcome"]] = outcomes.get(a["outcome"], 0) + 1
        kinds = {}
        for e in self.events:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        s = {
            "scenario": sim.name,
            "seed": sim.seed,
            "end_at": sim.end_at,
            "quiesce_at": sim.quiesce_at,
            "liveness_window": sim.liveness_window,
            "final_t": sim.now,
            "expect": sim.expect,
            "actions": acts,
            "outcomes": dict(sorted(outcomes.items())),
            "all_resolved": all(a["outcome"] != "unresolved" for a in acts),
            "messages_sent": kinds.get("msg_send", 0),
            "event_counts": dict(sorted(kinds.items())),
            "trace_sha256": self.sha256(),
            "error": None if self.error is None else f"{type(self.error).__name__}: {self.error}",
        }
        return s

    def write(self, out_dir, summary_extra=None):
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "trace.jsonl"), "w", encoding="utf-8") as f:
            f.write(self.text)
        logdir = os.path.join(out_dir, "logs")
        os.makedirs(logdir, exist_ok=True)
        for name in sorted(os.listdir(logdir)):
            if name.endswith(".log"):
                os.remove(os.path.join(logdir, name))
        for name in self.sim._order:
            ns = self.sim._nodes[name]
            with open(os.path.join(logdir, f"{name}.log"), "w", encoding="utf-8") as f:
                f.write("".join(line + "\n" for line in ns.logs))
        s = self.summary()
        if summary_extra:
            s.update(summary_extra)
        with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2, sort_keys=True)
            f.write("\n")
        return s
