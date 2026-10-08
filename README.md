# FunSearch pack

FunSearch is a project-agnostic Gas City pack for evolutionary program search. Coding agents act as mutators, writing candidate programs that a user-supplied evaluator scores through a C interface. A C worker hosts the evaluator, while a Python engine manages the program database, search islands, tasks, and run lifecycle.

Status: implemented under epic mc-h4zx; the live cap-set acceptance passed
(see [docs/acceptance-capset.md](docs/acceptance-capset.md)).

```text
pack.toml                 Pack metadata
Makefile                  Worker build and test entry points
include/                  Evaluator C interface (funsearch.h)
worker/                   C evaluator host (funsearch-worker.c)
engine/funsearch/         Python search engine package
bin/                      funsearch CLI
agents/                   Mutator agent role
formulas/                 Run lifecycle formulas
orders/                   Crash-cleanup order
scripts/                  Gas City lifecycle hooks and sweep scripts
skills/                   Problem-setup guidance
examples/                 Bundled problems, including cap set
docs/                     Design documentation
tests/                    Python suites and shell test runners
```

Run `make` to build the worker, `make test` to build it and run the engine, Gas City, worker, skill and example suites, and `make clean` to remove build output. Build artifacts go in `build/`; run artifacts belong in `runs/`. The engine also builds a missing or stale worker on first use, under a file lock with an atomic install, so a read-only pack needs a prebuilt, current `build/funsearch-worker`.

Licensed under Apache-2.0; see [LICENSE](LICENSE).

## Setting up a problem

Use the [problem-setup skill](skills/funsearch-problem-setup/SKILL.md) and its
copyable templates to write the candidate interface, seed, evaluator, and
configuration. Export `FS_PACK` to this pack's absolute path so the template
evaluator can find `include/funsearch.h`. Validate with
`"$FS_PACK/bin/funsearch" check /absolute/path/to/problem` before launching.

## Running a search

Once the FunSearch pack is imported into your rig, sling the run formula to a
general coding-agent pool in that rig. Replace the angle-bracket arguments
with your rig, pool, and completion-mail recipient:

```sh
gc sling <rig>/<pool> funsearch-run --formula \
  --var pack=/absolute/path/to/funsearch-pack \
  --var problem=/absolute/path/to/my-problem \
  --var instance=n=6 --var notify=<recipient> \
  --var 'overrides=search.mutators=3 stop.duration_s=3600 stop.max_children=100'
```

Omit `instance` and `overrides` to use the problem defaults. `pack` is the
absolute path of this pack (the directory containing `pack.toml`); the start
step runs its CLI from there. Overrides may set
only `search.*` and `stop.*`; use the separate `instance` variable for the
instance and `problem.toml` for the mutator model. Keys that hold shell
commands (`candidate.compile*`, `evaluator.build`) come only from the problem's
`problem.toml`. The seed must score
`FS_OK`. For the copied evaluator template, set `evaluator.build` to
`make -C evaluator FS_PACK=/absolute/path/to/funsearch-pack` so an agent can
build it without inheriting your shell environment. Results appear in
`<problem>/runs/<run-id>/`: `best.c` and
`summary.json` at completion, with a summary on the run bead and completion
mail to `notify`. The formula records the run id and directory on its start
step and returns while the engine and mutators continue. `notify` is required:
Gas City does not export a reliable slinger recipient. Formula compiler v2
must be enabled. Run the formula on a general coding pool; mutator slots are
routed directly to `<rig>/funsearch.mutator` with `--no-formula`.

The start hook creates a `funsearch-run` bead in the importing rig and K child
`funsearch-slot` beads, carrying N and the configured mutator model. It stores
bead ids and rig/notify context in the run's `gc-lifecycle.json` (the engine's
`run.json` manifest stays write-once), and registers the run (directory, run
bead, rig and notify) at `<city>/.gc/funsearch/active/<run-id>.json`. Run ids
must be unique across the city. At completion the finish hook records best versus seed, children scored,
throughput, OK rate, reason and `best.c`, closes slots before the run bead,
sends `gc mail` to `notify`, then removes the registry entry.

The `funsearch-sweep` exec order is dormant when the registry is empty. With
active runs it checks every controller tick but sweeps at most every 30
minutes. A dead engine without a terminal summary is marked failed with
reason `engine died`; `bin/funsearch run recover <run-dir>` rewrites its
outputs from the live database, or the newest readable snapshot if the engine
died mid-write. It refuses a live engine and a run whose summary is already
terminal, so it never relabels a finished run. A dead
engine with a terminal summary has its interrupted finish hook retried with
that outcome preserved. Live engines are left alone. Sweep errors retain the
entry for a later retry and appear in order output. The sweep always updates
`last-sweep`; a city lock prevents overlapping sweeps from different rigs.

Hooks serialize per run and checkpoint completed actions for repeat calls.
Sequential retries do not resend completion mail. A process death between a
successful external write/mail and its local checkpoint can repeat that action;
Gas City and the local JSON registry do not share a transaction.
For a manual retry, use `<pack>/scripts/on-finish.sh <run-dir>`; saved context
selects the correct rig even from another cwd. Direct hook launches require
`GC_CITY_PATH`, `GC_RIG` (or `FS_CITY_PATH`, `FS_RIG`) and `FS_NOTIFY`.
`FS_GC=/absolute/path/to/gc` can select a test shim or an alternate CLI binary.


## Using the engine directly

The engine needs Python 3.11 or later, a C compiler, the ASan runtime,
`make`, and `flock`; its Python modules use only the standard library.
Every run initializes an ASan trial pool, even if no `try` calls are planned.
Check that `${CC:-cc} -print-file-name=libasan.so` resolves to an existing file.
`CC` selects this runtime discovery compiler independently of
`candidate.compile_try`. It also works without Gas City:

```sh
bin/funsearch check /path/to/problem --instance n=6
bin/funsearch run start /path/to/problem --run-id experiment \
  --set search.workers=2 --set stop.max_children=100
# start prints: <run-id> <absolute-run-dir>
bin/funsearch next-task /path/to/problem/runs/experiment --slot 1
# next-task prints: TASK <task-id> <absolute-task-dir>
bin/funsearch try /path/to/problem/runs/experiment 1 /path/to/task/child.c
bin/funsearch submit /path/to/problem/runs/experiment 1 /path/to/task/child.c
bin/funsearch run status /path/to/problem/runs/experiment
bin/funsearch best /path/to/problem/runs/experiment -k 5
bin/funsearch rescore /path/to/problem/runs/experiment best --instance n=7
bin/funsearch stop /path/to/problem/runs/experiment
# after an engine crash (refused while the engine is alive):
bin/funsearch run recover /path/to/problem/runs/experiment
```

`run start` checks the seed and returns after the daemon's worker pools are
ready. Configuration, the seed, the problem statement, and the candidate
header are saved in the run directory, together with a snapshot of the
evaluator library's directory (`<run>/evaluator/`) that scored the seed.
The run's workers and `rescore` load that snapshot, so rebuilding or fixing
the problem's evaluator never changes a run, and a rescore of an old run
uses that run's evaluator. `run.json`'s `evaluator_sha256` is a digest of the
whole snapshot, not a `sha256sum` of the library: `evaluator_digest` hashes
the sorted list of every file's SHA-256 and every symlink's target. (Earlier
builds of this pack stored the library file's plain SHA-256 under that key.)
Keep the library in its own small subdirectory, such as `evaluator/`. A
symlink in that directory must be relative and stay inside it, or `check` and
`run start` refuse the evaluator. Each try or submission compiles a
private source copy; includes of `candidate.h` work there and during rescore.
Candidate source may not include absolute or `..` paths, use computed
includes, `#embed`, inline assembly or `##` token pasting (see Security notes).
Try results print `RESULT <status> <score> <message>`; accepted submissions
print `ACCEPTED <program-id> <status> <score>`. The score is meaningful only
for status `OK`; a non-OK result without one prints `0`. A rejected submission leaves
its task open. A compile failure during try consumes a trial and records an
ERROR result. Invalid and crashing submissions are stored so future mutators
can see what was tried. Exact and normalized duplicates are rejected before
scoring; candidates with the same OK score and signature as an active
program are rejected after scoring. Sources that include headers or use
whitespace-sensitive preprocessing retain their source text for hashing,
so distinct macro arguments and source positions remain eligible for scoring.

The daemon stops for a requested stop, the duration limit, the submitted-child
limit, or the configured plateau. `children_scored` counts authoritative
submitted evaluations, including behaviour duplicates rejected after scoring;
tries and compile failures do not count. `summary.json` reports `duplicate_rate`
(behaviour-duplicate rejections divided by `children_scored`) and
`distinct_stored` (retained submission program rows, excluding seeds). The
maximum-child limit includes in-flight submissions so multiple workers cannot
overshoot it. Evaluations already running can finish for up to two minutes after stopping; queued
requests receive RUN_OVER. Runs export `summary.json`, `best.c`, and the top
ten distinct OK candidates in `top/`. Candidate exports are installed atomically
and flushed before the terminal summary is published; an interrupted export
leaves the run eligible for recovery and the crash sweep. If publication still
fails while run data is readable, the sweep retains its registry entry for a
later retry before sending completion mail. Recovery holds the engine's per-run
`recovery.lock` through queue repair and export publication and rechecks the
terminal summary after acquiring it. Concurrent manual recoveries and the sweep
wait for that owner and preserve its completed output. The final and periodic
SQLite backups live in `snapshots/`, with the latest five retained. Worker failure results
include the last 2 KiB of native stderr from a
continuously drained, bounded 1 MiB tail; daemon tracebacks appear in
`engine.log`. Both pools recycle workers after 100 scoring replies, before
the next request, to bound retained candidate state without memory-noise
restarts.

`search.workers` sizes each of the two pools, submit (final) and try
(sanitizer), and both start eagerly and stay resident for the whole run, so a
run holds `2 × search.workers` evaluator processes. Capacity does not move
between pools: idle try workers cannot score a burst of submissions. For a
heavy evaluator (cap-set embeds Julia with `memory_mb = 4096`), budget about
`2 × workers × evaluator RSS` per run, times the number of concurrent runs on
the host.

Run databases and snapshots use SQLite DELETE rollback journaling and a
5000 ms busy timeout. WAL is avoided because its shared-memory mmap can
SIGBUS on host-mounted run directories (observed on a Docker Desktop host
mount). Writable opens convert existing WAL databases to DELETE; stop old
engines/clients before migrating a legacy run. Status, best, rescore, and
mutator inspection open read-only connections without schema writes. The
database layout is versioned (`PRAGMA user_version`); only the engine
upgrades an older run, when it starts or when `run recover` runs after it
exits. `next-task`, `try`, `submit` and `stop` refuse a run whose layout
differs, rather than change it under an older engine that is still running.

Rollback journaling makes readers and the writer exclude each other, and
SQLite is the only channel between clients and the engine (clients poll
results every 0.1 s; the mutator tool guard opens the database on each tool
call). The engine retries a tick whose database access stays locked past the
busy timeout and fails the run only after 60 s without a successful tick.
Snapshots normally copy 1024 pages per step and pause between steps, so writers
wait for a step. After three restarts caused by client writes, the backup
finishes in one pass that blocks writers for the whole copy. A failed copy
leaves no partial snapshot. Both copy modes stop retrying a locked source at a
shared monotonic deadline based on the connection's busy timeout, allowing the
engine's elapsed retry budget to apply. This is sized for one host and a handful of mutators
per run (the default is three); much larger mutator counts or very large source
histories would need measuring first.

The engine flushes timestamped startup, shutdown, and SIGTERM/SIGHUP/SIGINT
events to `engine.log`; those signals request normal shutdown. Python's
faulthandler writes fatal-signal stack traces there. A small detached parent
waits for the engine and writes `engine-exit.json` with its PID, end time,
exit code, and signal (including SIGKILL). This evidence requires that the
parent survives; a SIGKILL record alone does not establish an OOM cause.
Liveness does not depend on that record or on a PID: the engine holds an
exclusive `flock` on `engine.lock` in the run directory for its whole life,
and the kernel releases it however the engine dies, including a loss of the
whole process tree (container or host restart, cgroup OOM, process-group
kill) where no exit record is written. Waiting clients, `run status` (its
`engine_alive` field), `run recover` and the Gas City sweep probe that lock, so
an unrelated process that later reuses the PID never looks like the engine. A
run directory without `engine.lock` has no live engine. The PID copies
(database `pid` state, `engine.pid`, run-bead `fs.pid`) only name the engine.
The detached observer execs without the launching agent's session identity,
so Gas City orphan cleanup cannot mistake the search for a retired agent.
City, rig, and notification context remain available to lifecycle hooks.

Optional `--on-start 'command'` and `--on-finish 'command'` hooks receive the
absolute run directory as an argument and in `FS_RUN_DIR`. The engine daemon
runs both, logging their output to `engine.log`: the start hook after its
workers are ready and before any evaluation is dispatched (`run start` returns
once it finishes, and fails the run if it fails), the finish hook after outputs
are written, including when the daemon fails. Each hook must exit within
120 s (`HOOK_TIMEOUT_S`); on timeout the engine kills the hook's whole process
group and treats it as a failure, so a slow start hook fails the run. The PID
file is removed after shutdown and the finish hook completes. Finish-hook failures
preserve the published search outcome and record the delivery or cleanup error
in `finish-hook-error.json` and `engine.log`; the Gas City sweep can retry delivery.

CLI exit codes: 0 success; 1 runtime or seed failure; 2 usage/configuration
error; 3 RUN_OVER for next-task/try/submit; 4 rejection or exhausted trial
budget. `run status`, `best`, and `rescore` remain available after completion.

## Mutator role

`funsearch.mutator` is a demand-driven Claude pool using
`claude-haiku-4-5-20251001`. Each fresh session claims a routed slot bead,
reads its metadata through `bin/funsearch slot show <bead>`, and performs
`next-task` → read TASK.md → write child.c → `try` → `submit`.
Trials obey the task budget; duplicate rejection leaves the same task open.
After N accepted submissions it releases the slot and drains. RUN_OVER
(exit 3 from any task command) closes the slot and drains.
`slot show` also reports an unfinished task and its used trials so a fresh
session can resume after a crash or error without allocating a second task.

N is `[search] tasks_per_session`, copied to `fs.tasks_per_session` on each
slot bead; it defaults to 5 accepted submissions per mutator session. K is
`[search] mutators`, the number of slot beads per run.
The agent's `max_active_sessions = 3` supports the default K=3. That cap is
shared by every concurrent run in the rig, so a run's effective mutator
concurrency is `min(search.mutators, cap minus other runs' sessions)`: slots
beyond it wait for a free session. The on-start hook warns (in `engine.log`)
when `search.mutators` alone exceeds the pack's cap. For larger K, raise the
rig's agent patch cap too, allowing for other concurrent runs and
rig/workspace limits. `[mutator] model` is passed by the run lifecycle hooks
as slot `opt_model` metadata to override the agent default. Slot metadata must
also include `fs.run_dir` (absolute) and `fs.slot` (string or integer).

The agent's working directory is private per concrete session, under
`<city>/.gc/funsearch/mutators/`. `pre_start` installs a Claude `PreToolUse`
guard. Claude runs in `dontAsk` mode with only Bash/Read/Write/Edit, project
settings, no MCP servers, and slash commands disabled. The guard permits
only literal calls to this pack's next-task/try/submit and slot helpers,
`gc hook --claim --json` (optionally `--drain-ack`), and
`gc runtime drain-ack`. It rejects shell composition/substitution, other
commands, wrong runs/slots/tasks, and file access outside the current task's
TASK.md and child.c. Only child.c is writable. Paths with shell metacharacters
are unsupported; spaces may be quoted. The guard grants explicit permission
for allowed calls; all others are denied.

`slot release` uses an ownership/status-guarded `gc bd update` to reopen and
unassign the bead, clear stale session/claim/work-directory metadata, and preserve
`gc.routed_to`. `slot close` uses the same guards with `--status=closed` so
ownership and the terminal update happen atomically. An already closed slot
(for example, by the finish hook) is a read-only success. Neither helper drains;
the agent calls `gc runtime drain-ack` only after a successful transition.
`bin/funsearch` hands its `slot` verb to the Gas City layer
(`scripts/gc_lifecycle.py`); the engine package itself never calls Gas City.
After a successful release or close, a session receipt makes the tool guard
permit only `gc runtime drain-ack` for that retiring session. This prevents
late nudges from reusing its context before the controller completes the drain;
a new pool session can claim normally.

## Security notes

### Trust model (v1)

Candidate C programs written by LLM mutators are compiled and run natively as
the invoking user, with that user's filesystem and network access. The
mutator tool guard and compile source policy keep honest mutators on task and
catch accidents. They are **not a security boundary** and do not contain
adversarial code. Use v1 only for trusted local experiments. OS-level isolation
is future work, tracked in backlog bead `mc-v8f3.5`.


The mutator guard enforces a Claude tool policy, not process isolation, and
it is **not a boundary against the mutator itself**. Gas City's agent schema has
no OS filesystem or network sandbox setting. The guard must allow `try` and
`submit`, and those compile the mutator's C and run it inside an evaluator
worker with the host user's access. Such code (for example a constructor) can
read or write anything that user can, including the session's generated
`.claude/settings.json`, its slot context, run state and the city. TASK.md
embeds other candidates' source, so a prompt injected there can steer a mutator
into writing such code. Symlink swaps by another process remain a race. Do not
use this setup as an adversarial evaluator-hiding boundary.

Best-effort hygiene for trusted local experiments:

- The compile source policy is a best-effort lint. It rejects absolute or `..`
  includes, computed includes, `#embed`, inline assembly (`asm`, `__asm__`),
  `##` token pasting, and `incbin`/`.include` text before compiling. It may
  reject inert text and may miss compiler-specific forms; it does not isolate
  compilation or prevent arbitrary filesystem reads or diagnostic disclosure.
- Candidate code (evaluator workers and the export check's library load)
  gets an allowlisted environment: `PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`,
  `LANGUAGE`, `LC_*`, `TZ`, `TMPDIR`, `LD_LIBRARY_PATH`, plus the patterns in
  the problem's `evaluator.env`. Gas City identity, store scope, agent sockets
  and credentials are not passed on unless a pattern names them.
- Each scoring request carries a random token that a reply must echo, and
  the worker never keeps the request in a stdio buffer. After each reply the
  engine sends a `SYNC` barrier; any extra line before its echo (such as the
  worker's genuine reply following a forged one) scores ERROR and replaces the
  worker, and output arriving after the barrier replaces the worker before the
  next request. Candidates share the worker's address space, so a determined
  candidate can still find the token, intercept the barrier or suppress the
  worker's own reply: this raises the bar against accidental or casual reward
  hacking and is not an isolation boundary.

These are hygiene measures within the trusted-local model. V1 does not support
untrusted runs; OS-level isolation remains future work (`mc-v8f3.5`).

The pack and generated settings must remain trusted, and a launch override
must not disable project settings/hooks, enable permission bypass, or add
tools/MCP servers. Gas City also supplies its city-managed Claude hooks;
their trusted lifecycle commands are outside the model's tool allowlist.
The live cap-set acceptance ([docs/acceptance-capset.md](docs/acceptance-capset.md))
verified the actual Claude launch and hook loading. Policy/helper tests here
exercise both allowed and denied calls.
