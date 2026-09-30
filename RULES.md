# Rules

## Model proxy

- **Models:** `anthropic/claude-sonnet-4.6` on the chat routes (`/v1/chat/completions`, `/v1/messages`), and Jev
  (`typesafe/jev-1.13` or `~typesafe/jev-latest`) on the Decisions route (`POST $JEV_DECISIONS_URL`, the
  same request format as OpenRouter's `/api/alpha/decisions`). Any other model is refused (403). Thinking budgets up to
  16k tokens are allowed. Web search, plugins, provider routing and server-side tools are refused (400).
  Other unrecognised request fields are dropped.
- **Spend:** the proxy charges your case budget at actual cost and refuses a request that could take you
  over it (402).
- **Prompt caching** is applied automatically. Cached input costs a tenth of normal input.

## Running locally

`grade.sh` accepts either a Docker image or a directory with an executable `run.sh`. Both get the same
inputs as described in `README.md`. Real grading uses your image, so grade the image before you submit.
Build with `--platform linux/amd64` on Apple Silicon.

## Scoring

- **A bug is fixed** when its targeted scenario passes on every hidden seed with no invariant violations
  (`python -m sim check` runs the same invariants, and `INVARIANTS.md` in each case describes them). In a
  chained case, fixing a later bug counts only once the earlier bug is fixed too.
- **A patch that breaks the service's tests** (public or private regression) scores 0 for that case. Only tests that pass on the unfixed code, and still pass when any subset of the case's bugs is fixed, count here, so a correct partial fix is never zeroed by a test that another unfixed bug breaks.
- **Headline:** the number of hidden bugs fixed. **Tie-breaker:** total fixer time across hidden cases.
- The simulator is deterministic, and a patch that makes replays nondeterministic fails.

## Practice

- `./grade.sh --fixer <dir-or-image> [--cases practice/<ID>]` grades exactly as we do, using the practice
  graders. Run it as often as you like, within your $150 practice budget.
- Hidden cases use unseen codebases and service docs. Don't tailor your fixer to the practice services.

## What we look at

We read your fixer's source, its patches and logs, and your chat logs, and then discuss them with you in
the interview. Fixing bugs by changing the simulator, the tests or the grader doesn't count, and it'll be
obvious.
