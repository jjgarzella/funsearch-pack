# Live cap-set acceptance — FAIL

Date: 2026-10-05 UTC. Acceptance bead: `mc-h4zx.11`; rig:
`math-experiments`. This is a real formula launch with real Haiku mutators,
beads, and completion mail. It does **not** satisfy the epic's acceptance gate.
The acceptance bead remains open; successors must not be dispatched.

## Run and outcome

The pack was imported beside gasvillage in the math-experiments rig after
backing up city.toml to `/tmp/city.toml.bak-mc-h4zx.11`. Only the three import
lines were added; city.toml was not committed. `gc config show --validate`
passed, `gc reload --json` succeeded, and `gc agent list` showed
`math-experiments/funsearch.mutator`. The import remains installed.

The authoritative pack pre-flight returned `OK`, score 64, signature
`[16,32,64]`, after the worker fix described below. Launch command:

```sh
gc sling math-experiments/codex funsearch-run --formula \
  --var problem=/home/jjgarzella/Developer/ai/city/math-city/packs/funsearch/examples/cap-set \
  --var instance=n=6 --var notify=kurt \
  --var 'overrides=stop.duration_s=3600 stop.max_children=100 search.mutators=3' \
  --json
```

Workflow `mx-9d4`, start step `mx-xd2`, launched run
`20261005-231022-e9c4` from pack commit `db3bfe1`. Run artifacts are under
`examples/cap-set/runs/20261005-231022-e9c4/` in the authoritative checkout.
The run bead is `mx-bei`, its slots are `mx-bei.1` through `.3`, and its
engine PID was 71947. Configuration used four islands, two workers, three
mutators, N=1, and three trials per task.

The engine died without a traceback or terminal summary after four successful
trial evaluations and before any authoritative submission finished. The
last completed evaluation ended at 23:15:07.415 UTC; subsequent requests
remained queued. A forced sweep recovered a `failed` summary with reason
`engine died`. The recorded start is 23:10:31.528 UTC; the recovered end is
23:17:44.746 UTC, a 433.219-second interval including detection/cleanup delay.

| Acceptance criterion | Result |
| --- | --- |
| Unattended completion by a stop condition | **FAIL:** unexpected engine death; manual sweep required |
| Best score strictly exceeds seed | **FAIL:** 64 = 64 |
| Genuine recorded scores | PASS for all available records; no ten evolved candidates existed |
| Run/slots closed, summary note, mail, registry removal | PASS after cleanup fix and manual retry |
| Release to a fresh session's first next-task measured | **UNAVAILABLE:** no completed tasks or successful fresh replacement |
| Explicit engine kill followed by crash sweep | PASS after cleanup fix |

Final city reads confirmed the run bead closed with `fs.status=failed` and a
summary note, all three slots closed, and no remaining active registry entry.
The persisted lifecycle checkpoint records `mail_sent=true` and
`finished=true`; the successful sweep sent the completion mail to `kurt`.

## Metrics and session observations

| Metric | Value |
| --- | ---: |
| Children scored / children OK | 0 / 0 |
| Summary OK rate | 0; no submitted-child denominator |
| Submitted-child throughput/hour | 0 |
| Allocated tasks / completed tasks | 3 / 0 |
| Reserved try calls | 9 |
| Recorded trial results | 4, all OK (100% among completed trials) |
| Mean reserved tries per allocated task | 3.0 |
| Mean recorded trials per allocated task | 1.333 |
| Evaluations still queued at death | 8 (five trials, three submissions) |

Mean tries per **completed** task is undefined because none completed.
The queued requests do not count as scored children or recorded results.

The three real Claude launch commands selected `claude-haiku-4-5-20251001`,
`--permission-mode dontAsk`, Bash/Read/Write/Edit, strict empty MCP config,
project settings, and disabled slash commands. The project PreToolUse guard
was installed, and live Read/Write/try calls reached the assigned tasks.
Session IDs were `mc-wisp-hklx75`, `mc-wisp-sa4uv9`, and `mc-wisp-o3ugmr`.

Session creation timestamps from `gc session list` and engine task timestamps
give these initial startup costs:

| Slot | Session created UTC | First next-task UTC | Seconds |
| --- | --- | --- | ---: |
| 1 | 23:12:36 | 23:14:25.380 | 109.380 |
| 2 | 23:12:37 | 23:14:29.843 | 112.843 |
| 3 | 23:12:36 | 23:14:38.138 | 122.138 |

Mean initial startup was **114.787 seconds**. This includes scheduling,
provider initialization, initial prompt/claim handling, and task allocation;
it is not a measurement of model inference alone.

`gc bd history <slot> --events --json --rig math-experiments` recorded release
to later re-claim gaps of 69, 56, and 101 seconds. Those claims reused the
three existing session IDs, and resumed open tasks instead of creating new
ones. They are **not** fresh-session release-to-next-task measurements.
`gc session logs` could not locate the provider transcripts; terminal capture,
session inventory, bead audit events, and SQLite timestamps supplied evidence.

Provisional recommendation: test **N=5** once the engine failure is resolved.
The measured initial startup proxy would amortize to about 23 seconds per
task at N=5 versus 115 seconds at N=1. This is not a validated tuning result:
an acceptance rerun must measure actual fresh-session replacement cost and
completed-task throughput before changing the default.

## Independent verification and best function

The formula documents neither an environment-passthrough variable nor a way
to select its generated run directory, so the acceptance launch did not set
`FS_CAPSET_DUMP=<run_dir>/caps`. The allowed rescore route was used instead.

`bin/funsearch best <run_dir> -k 10` returned one distinct candidate, the seed.
`bin/funsearch rescore <run_dir> best --instance n=6` and rescoring all four
island seed IDs (0–3) each returned OK, score 64, signature `[16,32,64]`.
Each of the four completed trial evaluations was independently recompiled
from its immutable request source and scored through a fresh normal worker
pool; status, score, and signature all matched the stored trial result.

Verification enabled dumps into `<run_dir>/rescore-caps`. All **27** generated
caps passed `python3 tools/check_cap.py <run_dir>/rescore-caps`. This verifies
the rescored objects; the original live run did not dump its caps.
`acceptance-rescore.json` and `acceptance-metrics.json` preserve the results
inside the ignored run directory.

The best priority function remained the seed:

```c
#include "candidate.h"

// IDEA: seed — constant priority gives the lexicographic greedy cap.
double priority(const int8_t *v, int32_t n)
{
    (void)v;
    (void)n;
    return 0.0;
}
```

## Crash test

A second formula workflow `mx-6kt`, start step `mx-s5e`, launched
`20261005-232119-e020` with `stop.max_children=50 search.mutators=3`.
Its run bead is `mx-lz3` and its three slots are `mx-lz3.1` through `.3`.
After the start hook finished and all slots were routed, `/proc` showed
engine PID 9061 had PPID 1 (`docker-init`). The ancestry is saved in that
run's `acceptance-ancestry.json`.

Executed `kill -9 9061`, then
`gc order run funsearch-sweep --rig math-experiments`. The order exited 0;
the recovered summary reports `failed` / `engine died`, the run and slots
are closed, completion mail was sent, and the registry entry was removed.
No live FunSearch engine or worker processes remained after cleanup.

## Issues, fixes, and unresolved blocker

1. **Interactive embedded-Julia stdin failure — fixed in `db3bfe1`.**
   Pre-flight originally failed with `stdin read failed`. A syscall trace
   showed Julia changed the input pipe to nonblocking mode: the worker read
   the blank startup request, then got EAGAIN while awaiting another request.
   The C host now restores blocking stdin after evaluator initialization.
   An interactive cap-set regression waits between requests and verifies
   startup, scoring, and QUIT; the earlier queued-input tests missed this.
2. **Claimed-slot cleanup ownership — fixed in `8c99686`.**
   The first sweep was refused because `order:funsearch-sweep` did not own an
   active slot. Terminal cleanup now uses `gc bd close <slot> --force` for
   slots after checking for a terminal summary. The fake CLI now enforces
   external ownership, and the finish regression exercises claimed slots.
   Both live cleanup and the explicit crash test passed after landing.
3. **Launcher startup friction — observed and cleared.**
   The first Codex launcher was drained for CopyFiles config drift. Its
   replacement waited on folder trust and four generated lifecycle hooks.
   The hooks were reviewed (gc prime/handoff/nudge/mail commands), trusted,
   and `gc reload --soft --json` succeeded. Later formula launch required
   no manual trust intervention.
4. **Unexpected first engine death — unresolved acceptance blocker.**
   The log was empty and the PID vanished before a summary. Initial
   suspicion of launcher teardown was contradicted by later evidence:
   city events show the launcher kill at 23:12:44.334 while trial evaluations
   continued until 23:15:07, and the second daemon detached to PID 1.
   The cgroup reported `oom_kill=3` and a roughly 15.96 GB memory peak, but
   no before-run counter baseline was captured. Kernel logs were inaccessible.
   OOM is a possible explanation, **not an established cause**. Diagnose this
   with a fresh run and resource/process monitoring before repeating acceptance.
5. **Fresh replacement semantics — unverified.**
   After error-path release/drain, queued nudges let existing provider sessions
   reclaim slots before shutdown. No N accepted-submission boundary occurred,
   so this run establishes neither fresh-context recycling nor its startup cost.

## Validation and disposition

In the isolated `fs/mc-h4zx.11` worktree, after both fixes:

```sh
make worker && make -C examples/cap-set/evaluator && make test
```

Exit 0: 100 Python tests, 21 Julia exact checker/reference assertions, nine
cap-set process checks, the skill-template check, and 13 worker checks passed.
The claimed-slot lifecycle suite also passed separately (14 tests).
`git diff --check` passed. Logs are
`/tmp/fs-mc-h4zx.11-final-make-test.log` and
`/tmp/fs-mc-h4zx.11-lifecycle-fix.log`.

These prescribed direct/embedded Julia gates are the justified fallback:
the managed Kaimon allow-list permits the original DeRham checkout, not this
FunSearch worktree. No shared Julia session or service was changed.

The report and fixes land only on local pack main. No push, math-city commit,
or math-experiments commit was made. `kurt` received the blocker, corrected
diagnosis, and final acceptance summary. Keep `mc-h4zx.11` open until an
unattended improving run and fresh-session measurements pass; do not advance
the close-out or review beads on the strength of these offline tests.
