# C3 engineering take-home: build an automated fixer

C3 runs distributed GPU compute. Our services fail in the ways distributed systems fail: lost messages,
crashed workers, skewed clocks, and retries that do more harm than good.

**Your task:** build a program (a *fixer*) that repairs distributed-systems bugs **on its own**. We'll run
your frozen fixer, hands-off, on **12 hidden cases**. Each is a small service you've never seen, with one
or more bugs. **Your score is the number of hidden bugs your fixer fixes, and the total time it takes is
the tie-breaker.** We also review the quality of the fixer you build and go through it with you on a call.

Your fixer is a black box. Below is what goes in and what must come out. What's inside is up to you: one
prompt, a team of agents, an agent framework, custom tools, whatever works. At run time it may call only
**Claude Sonnet 4.6** and **Jev** (TypeSafe's fast yes/no decision model), through the model proxy. You can
use any tools and models while *building* it.

## What goes in

We run your fixer once per case, in a fresh container, with no human input and nothing carried over from
other cases.

**The case**, at `/workspace` (`$C3_WORKSPACE`), writable:

| Path | What it is |
| --- | --- |
| `SYMPTOM.md` | A terse incident ticket or on-call backlog. It doesn't say how many bugs there are |
| `logs/` | Logs from the failing run |
| `src/` | The service's source: **the only thing that is graded** |
| `tests/` | Public unit tests. They pass on the unfixed code and on a full fix |
| `sim/` | A deterministic simulator: `python -m sim replay --scenario public --seed N`, `python -m sim check --trace ...` |
| `scenarios/` | The failing scenario, fault files and templates. Vary seeds and faults to reproduce |
| `INVARIANTS.md` | What `sim check` enforces |
| `README.md`, `scenario_schema.md` | The service overview, and how to write your own scenarios |
| `case.json` | Case id, time and model budgets, public seeds |

**The corpus**, at `/corpus` (`$C3_CORPUS`), read-only: about 26M tokens of platform contracts, service
docs and background material. Start at `INDEX.md`. Many cases depend on a fact that is only in the corpus.

**The environment:**

| Variable | Meaning |
| --- | --- |
| `C3_DEADLINE` | Unix time (seconds) when your fixer is stopped |
| `C3_BUDGET_USD` | Model spend available for this case |
| `ANTHROPIC_BASE_URL` | Model proxy, Anthropic format (append `/v1/messages`) |
| `OPENAI_BASE_URL`, `OPENROUTER_BASE_URL` | Model proxy, OpenAI format (already ends in `/v1`) |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` | A per-case proxy token (not a real key) |
| `ANTHROPIC_MODEL` | `anthropic/claude-sonnet-4.6` |
| `JEV_DECISIONS_URL`, `JEV_MODEL` | Jev's Decisions API through the proxy (`typesafe/jev-1.13`). See [Jev on OpenRouter](https://openrouter.ai/docs/guides/community/jev) |

Standard Anthropic and OpenAI SDKs work pointed at these.

**Limits:** 15 minutes per case, $3 of model spend per bug in the case, 2 CPUs, 4 GB RAM, and no network
except the proxy. Over budget, the proxy returns 402; any other model returns 403. Jev calls come out of the
same budget but cost almost nothing (about $0.04 per million input tokens, output free).

## What must come out

- **Edits under `/workspace/src/`.** We diff `src/` against the original and grade that. Anything else you
  write is discarded.
- **Exit before `C3_DEADLINE`.** At the deadline we send SIGTERM, then SIGKILL 5 s later, and grade whatever
  is in `src/` at that moment, so keep it valid as you go.
- Logs on stdout/stderr are optional. We keep them and read them when reviewing.

A bug counts as fixed when its hidden scenario passes on all 20 hidden seeds. A patch that breaks the
service's tests scores 0 for that case. Details are in `RULES.md`.

## Quick start

```sh
# 0. Python >= 3.12 with PyYAML and pytest (and Docker for image submissions)
python3 -m pip install pyyaml pytest

# 1. Put your OpenRouter key (supplied by C3; $150 practice budget) in .env
echo 'OPENROUTER_API_KEY=sk-or-...' > .env

# 2. Start the local model proxy. It enforces the same model, budget and caching rules as real grading.
#    It reads .env only when it starts, so restart it if you change the key.
python3 proxy/server.py &

# 3. Check the plumbing with the no-op fixer. It changes nothing and calls no model,
#    so expect "0/2 bugs fixed" and $0.00.
./grade.sh --fixer fixers/examples/noop --cases practice/A2

# 4. Run the base LLM example: one Sonnet 4.6 call through the proxy (about $0.20).
#    It rarely fixes anything; it shows how a fixer talks to the model. Your fixer starts here.
./grade.sh --fixer fixers/examples/minimal-llm --cases practice/A2

# 5. Before you submit: build your fixer as an image and grade the image, exactly as we will
docker build --platform linux/amd64 -t my-fixer fixers/examples/minimal-llm
./grade.sh --fixer my-fixer --cases practice/A2
```

Results go to `results/<timestamp>/<case>/result.json` (`bugs_fixed` is what counts). Each case's folder
also has `fixer.log` (your fixer's output), `patch.diff` (the graded change) and `eval.json` (per-seed
results).

## The pack

- `practice/`: 6 practice cases (codebases A and B). The hidden cases use three other codebases (C, D, E).
- `grading/`: the practice graders, which work exactly like real grading.
- `corpus/`: the practice documentation corpus.
- `fixers/examples/`: example fixers, each with a `run.sh` and a `Dockerfile`. `noop` and `trivial` show the
  wiring. `minimal-llm` is the simplest fixer that uses the model: one Sonnet call through the proxy, no
  simulator or corpus use, no checking. It rarely fixes anything; it's a starting point, not a strategy.
- `RULES.md`: scoring details, proxy request rules, and what we check.

## Submitting

**Test your submission first.** Grade the exact image you'll send on every practice case:

```sh
./grade.sh --fixer my-fixer        # no --cases: all practice cases
```

This runs the image the way we run it on the hidden cases: the same time and budget limits, the same model
proxy and the same scoring. If it builds, runs and scores here, it will run for us. The practice score is a
guide only, because the hidden cases use codebases you haven't seen.

Send us:

1. Your fixer as a `linux/amd64` Docker image (≤ 4 GB, a `docker save` tarball or registry reference) whose
   entry point runs it with no arguments, plus the source directory and `Dockerfile` it's built from.
   Install all dependencies at build time. The image needs Python ≥ 3.12 with PyYAML and pytest.
2. Your agent chat logs from building it. These are for context and aren't scored.
3. A half-page write-up: how your fixer works and what you'd do next.

You have **7 days** from receiving the pack. We expect it to take 2–3 hours. More time is fine but
not expected.
