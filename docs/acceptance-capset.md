# Live cap-set acceptance — FAIL

Date: 2026-10-05 UTC. Acceptance bead: `mc-h4zx.11`; rig:
`math-experiments`. This is a real formula launch with real Haiku mutators,
beads, and completion mail. It does **not** satisfy the epic's acceptance gate.
The acceptance bead remains open; successors must not be dispatched.

Latest diagnosis: the instrumented rerun below exited with **SIGBUS (7)**
inside SQLite `get_state`; the OOM-kill counter remained **3 → 3**.
This rerun does not support the OOM hypothesis. The underlying SIGBUS cause
and the original uninstrumented death remain unresolved.

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

## Instrumented rerun: SIGBUS in SQLite, no OOM counter increase

Kurt requested one instrumented rerun in the bead's 23:30/23:33 UTC notes.
Local pack commit `dc8778c` adds flushed UTC lifecycle events, logged
SIGTERM/SIGHUP/SIGINT handlers that request normal shutdown, and Python
faulthandler fatal-signal traces. A small detached observer waits for the
engine and records its exact exit code/signal in `engine-exit.json`, including
SIGKILL. The observer owns no evaluator workers or database connection.
Fatal traces and exit evidence require the relevant processes to survive long
enough to write; they cannot by themselves establish an OOM cause.

The unchanged-parameter launch was:

```sh
gc sling math-experiments/codex funsearch-run --formula \
  --var problem=/home/jjgarzella/Developer/ai/city/math-city/packs/funsearch/examples/cap-set \
  --var instance=n=6 --var notify=kurt \
  --var 'overrides=stop.duration_s=3600 stop.max_children=100 search.mutators=3'
```

Workflow `mx-jms`, start step `mx-n51`, launched run
`20261005-234002-641c` using pack `dc8778c`. Its run bead is `mx-0da`;
slots are `mx-0da.1`, `.2`, and `.3`. `run.json` confirms N=1,
`search.workers=2`, and `evaluator.memory_mb=4096`. The actual worker count
key is `search.workers`, not `evaluator.workers`; the daemon creates separate
submit and try pools, so this means four evaluator workers. The seed preflight
again returned OK 64, signature `[16,32,64]`.

Before dispatch, `/tmp/fs-mc-h4zx.11-instrument-baseline.log` recorded
`oom_kill=3` at 23:37:15 UTC. One detached shell sampler attached to the new
run directory and appended UTC time, cgroup memory events, `free -m`, the ten
largest RSS processes, and engine PID/liveness to `mem.log` every 15 seconds.
It stopped after crash cleanup produced `summary.json`. A separate single
120-second shell monitor waited for that summary, with a 100-minute deadline
and status output every ten minutes; it completed normally after cleanup.

Engine PID 66099 started at 23:40:06 UTC, with observer parent 66098, and
reported its worker pools ready at 23:40:11. It died at
**23:43:39.513216 UTC**, 213.293 seconds after the persisted run start.
The observer's durable record is:

```json
{
  "pid": 66099,
  "ended_at": 1791243819.513216,
  "exitcode": -7,
  "signal": 7
}
```

The corresponding `engine.log` contains:

```text
Fatal Python error: Bus error
Current thread ... (most recent call first):
  File ".../engine/funsearch/db.py", line 318 in get_state
  File ".../engine/funsearch/daemon.py", line 166 in serve
  File ".../engine/funsearch/daemon.py", line 331 in daemonize
  File ".../engine/funsearch/cli.py", line 128 in start_run
2026-10-05T23:43:39Z pid=66098 engine pid=66099 exited exitcode=-7 signal=7
```

`get_state` was executing `SELECT value FROM state WHERE key=?`. The other
reported Python thread was idle in `concurrent.futures.thread._worker`.
No caught TERM/HUP/INT event precedes the failure.

The sampler excerpt bracketing the death is summarized below; memory columns
are the MiB values printed by `free -m`. `mem.log` retains the full process
lists and cgroup counters.

| UTC sample | oom_kill | RAM used | RAM available | Swap used | Engine alive |
| --- | ---: | ---: | ---: | ---: | --- |
| 23:43:01 | 3 | 10,936 | 5,035 | 1,023 | yes |
| 23:43:16 | 3 | 11,125 | 4,846 | 1,023 | yes |
| 23:43:31 | 3 | 11,387 | 4,584 | 1,023 | yes |
| 23:43:46 | 3 | 10,492 | 5,479 | 1,023 | no |

Relevant literal lines from those two `mem.log` samples (intervening memory
and process lines omitted):

```text
2026-10-05T23:43:31Z
oom_kill 3
engine_pid=66099 alive=yes
2026-10-05T23:43:46Z
oom_kill 3
engine_pid=66099 alive=no
```

All samples kept `oom_kill=3`: **delta 0**. Thus this observed engine death
was SIGBUS, not an OOM-killer SIGKILL. This evidence does not retrospectively
identify the original run's uninstrumented death. The requested reduced-worker
retry was conditional on an increased OOM-kill counter, so it was not run.

The run database enables SQLite WAL. `findmnt -T <run_dir>` reports the
`fakeowner` mount at `/home/jjgarzella/Developer/ai`, backed by
`/run/host_mark/Users[/jjgarzella/Developer/ai]`. There was 385 GB disk space
available and `/dev/shm` used only 8 KiB of 64 MiB. A WAL shared-memory mapping
or host-filesystem problem is a plausible investigation target given the
SQLite stack, **not a demonstrated root cause**. No database placement or
journaling changes were made during this diagnosis.

### Rerun metrics and verification

| Metric | Value |
| --- | ---: |
| Best / seed score | 64 / 64 |
| Submitted children scored / OK | 0 / 0 |
| Summary OK rate / throughput per hour | 0 / 0 |
| Allocated / completed tasks | 3 / 0 |
| Reserved tries | 6 |
| Recorded trials | 1, OK 64 |
| Mean reserved tries per allocated task | 2.0 |
| Mean recorded trials per allocated task | 0.333 |
| Mean tries per completed task | undefined |
| Queued requests at cleanup | 8: five tries, three submissions |

The eight unscored requests were enqueued after the engine's recorded death;
they do not represent scored results. Real Haiku sessions and restricted
launch settings were confirmed through `gc session list`. Initial creation
to first next-task costs, using initial claim actors from slot history and
SQLite task timestamps, were:

| Slot | Session | Created UTC | First next-task UTC | Seconds |
| --- | --- | --- | --- | ---: |
| 1 | `mc-wisp-db77a3` | 23:42:16 | 23:43:17.486566 | 61.487 |
| 2 | `mc-wisp-4yxsb9` | 23:42:16 | 23:43:21.181924 | 65.182 |
| 3 | `mc-wisp-hzzxi5` | 23:42:16 | 23:43:24.055762 | 68.056 |

Mean initial startup was **64.908 seconds**. No accepted-task boundary or
fresh replacement occurred; actual slot-release-to-fresh-next-task cost is
still unavailable. The provisional N=5 recommendation remains a hypothesis:
it would amortize this startup proxy to about 13 seconds per task, but cannot
be validated without successful completed tasks and replacement measurements.

`funsearch best <run_dir> -k 10` again returned only seed program 0.
`funsearch rescore <run_dir> best --instance n=6` and rescore of ID 0 both
returned OK 64, signature `[16,32,64]`. The sole completed trial's immutable
request source was independently recompiled with the final compiler and
rescored in a fresh normal worker; status, score, and signature matched.
All nine verification dumps passed the independent checker at the actual
repository path, `python3 tools/check_cap.py <run_dir>/rescore-caps`.
The best function remains the seed shown earlier. Verification records are
`acceptance-rescore.json`, `acceptance-trial-rescore.json`, and
`acceptance-metrics.json` inside the ignored run directory.

### Cleanup, checks, and disposition

`gc order run funsearch-sweep --rig math-experiments` exited 0. Recovered
`summary.json` is failed / engine died; the run bead closed with its summary,
all three slots closed, `mail_sent=true` and `finished=true` were checkpointed,
and the registry entry was removed. The engine, observer, evaluator workers,
sampler, and monitor no longer remained alive. The earlier explicit SIGKILL
crash test remains valid; this run additionally verifies recovery from a real
SIGBUS failure.

Before local landing of `dc8778c`, the exact gate
`make worker && make -C examples/cap-set/evaluator && make test` passed:
102 Python tests, 21 Julia assertions, nine cap-set checks, the skill-template
check, and 13 worker checks. The real CLI diagnostic suite also passed
separately (13 tests), exercising graceful TERM/HUP/INT shutdown, SIGKILL
exit evidence, and fatal ABRT stack traces. Logs are
`/tmp/fs-mc-h4zx.11-instrument-make-test.log` and
`/tmp/fs-mc-h4zx.11-instrument-tests.log`; `git diff --check` passed.
These prescribed direct/embedded Julia gates remain necessary because this
worktree is outside Kaimon's managed-project allow-list.

Acceptance remains **FAIL**: no unattended successful stop, no improvement,
and no fresh replacement measurement. Kurt was mailed the SIGBUS evidence
and OOM-counter delta. Keep the acceptance bead open and do not dispatch
successors. Only local pack commits were made; the city import remains in
place and nothing was pushed or committed in math-city/math-experiments.


## Host-mount WAL reproduction and rollback-journal fix

Coordinator direction on 2026-10-05 23:50/23:53 UTC requested a two-process
stdlib probe and replacement of WAL before another original-parameter run.
`/tmp/fs-mc-h4zx.11-wal-probe.py` initialized the same one-row WAL database
on each filesystem and ran a writer (UPDATE/commit loop) and a reader
(close/open/SELECT loop) concurrently for 90 seconds. The two filesystem
probes ran in parallel and ended at 23:53:06 UTC.

| Database location | Writer exit | Reader exit | Writer iterations | Reader iterations |
| --- | ---: | ---: | ---: | ---: |
| Pack run directory, `fakeowner` host mount | **-7 (SIGBUS)** | 0 | died before counter output | 24,842 |
| `/tmp`, overlay filesystem | 0 | 0 | 338,759 | 1,370,238 |

The mounted writer's faulthandler trace points to its SQLite UPDATE/commit
line. This reproduces a mount-specific WAL SIGBUS without Julia or the
FunSearch engine; `/tmp/fs-mc-h4zx.11-wal-probe.log` retains the full trace
and process exit records. Together with the earlier engine SQLite trace,
it supports the coordinator's WAL shared-memory-mapping diagnosis. The
probe does not locate the failing mmap address or retrospectively prove
the original uninstrumented engine's cause.

Local pack commit `d3794d0` changes the shared writable Database wrapper to
`PRAGMA journal_mode=DELETE`, retaining the 5000 ms busy timeout and foreign
keys. A writable open migrates an existing WAL database. SQLite backup
destinations also use DELETE. Status, best, and rescore use read-only
`file:...?mode=ro` connections without schema writes; the mutator guard
and slot inspection already used read-only connections. The engine, start,
next-task, try, submit, stop, and crash recovery all use the shared writable
wrapper. No sweep script logic changed; the previously passed kill/sweep
gate is retained per coordinator direction.

Regression checks cover legacy WAL migration with committed data,
read-only write refusal and missing-file handling, snapshot journal mode,
and absence of `db.sqlite-shm`/`db.sqlite-wal` throughout a real ten-child
toy run. README and the DB code comment explain host-mounted run dirs.

The prescribed `make worker && make -C examples/cap-set/evaluator && make test`
passed: 104 Python tests, 21 Julia assertions, nine cap-set checks, the
skill-template check, and 13 worker checks. Final strengthened read-only
snapshot and toy assertions also passed in focused seven-DB and ten-child
integration checks. `gc lint` and `git diff --check` passed. Logs:
`/tmp/fs-mc-h4zx.11-delete-make-test.log`,
`/tmp/fs-mc-h4zx.11-delete-db-test.log`, and
`/tmp/fs-mc-h4zx.11-delete-toy-test.log`. Direct embedded Julia remains the
justified fallback because Kaimon excludes this exact project/worktree.


## DELETE rerun: improvement, then inherited-identity orphan cleanup

Formula `mx-88l`, start step `mx-y2c`, launched
`20261005-235719-ce28` from `d3794d0` with the original 3600-second /
100-child / three-mutator parameters (N=1, two workers per pool, memory4096).
Run bead `mx-6n8w`, slots `.1`–`.3`, engine23779, observer23774.
The memory baseline was oom_kill3 at23:55:58Z; sampling began at23:58:46Z
(the first background sampler did not survive the tool process group;
replacement used start_new_session). The run began23:57:20.996Z.

At00:03:42Z, the engine logged `received SIGTERM; requesting shutdown`.
The supervisor log directly records:

```text
session reconciler: reaped process-table orphan pid=23774 session=mc-wisp-88c3a5
session reconciler: reaped process-table orphan pid=23779 session=mc-wisp-88c3a5
```

`/tmp/fs-mc-h4zx.11-delete-supervisor.log` retains the evidence.
Core `cmd/gc/session_beads.go:sweepProcessTableOrphans` selects untracked
processes with the launching session's GC_SESSION_ID after its bead closes.
The detached engine and observer inherited that identity. Double fork and
setsid detached process groups but did not remove the process-table identity.
This is a confirmed second lifecycle bug, separate from the reproduced WAL
SIGBUS. The observer was reaped before it could write engine-exit.json.

Normal signal shutdown wrote a stopped/stop requested summary, ran the finish
hook without a forced sweep, closed run and slots, sent kurt completion mail,
and removed the registry. All memory samples kept oom_kill3 (delta0).
The stopped outcome occurred after381.402s,
not at the configured duration/child limit, so it does not pass unattended
configured-stop acceptance.

| Metric | Value |
| --- | ---: |
| Best / seed |79 /64 |
| Scored / OK submissions |27 /27 |
| OK rate |1.0 |
| Throughput/hour |254.849 |
| Stored evolved programs |2 |
| Tasks / completed |4 /2 |
| Reserved / recorded tries |12 /12 |
| Mean tries per task |3.0 |
| Initial startup, slots1/2/3 |39.478 /40.394 /41.805 seconds |
| Mean initial startup |40.559 seconds |
| Slot1 release to next-task |68.252 seconds, **same session/context** |

39 completed evaluation results (27 submissions, including duplicate rejects,
and12 trials) and all six stored programs were independently recompiled and
rescored; status, score, signature matched. Best plus all three available
distinct top candidates matched the CLI rescore; a top ten did not yet exist.
All147 generated exact cap dumps passed `tools/check_cap.py`.
`acceptance-rescore.json`, `acceptance-metrics.json`, and
`acceptance-slot{1,2,3}-history.json` preserve the verification.

The slot1 reclaim reused both its session ID and Claude session key after
N=1 completed task; a late nudge arrived before controller drain completed.
This is a confirmed fresh-context boundary bug. The next fix uses a successful
release/close receipt in the guard to permit only drain for that retiring
session; another pool session can claim normally. The next engine fix execs
the observer with launcher identity removed (preserving city/rig/notify),
and verifies /proc environments for observer and engine. Linux environment
scrubbing requires exec: changing os.environ after fork alone leaves the
initial process environment visible in /proc.

Original crash/sweep gate remains passed; no sweep logic changed. Neither
this improving run nor the offline gates waive the unattended-stop and
fresh-session measurement requirements. Another original-parameter run is
required after these fixes.
