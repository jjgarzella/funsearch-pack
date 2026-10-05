# FunSearch pack

FunSearch is a project-agnostic Gas City pack for evolutionary program search. Coding agents act as mutators, writing candidate programs that a user-supplied evaluator scores through a C interface. A C worker hosts the evaluator, while a Python engine manages the program database, search islands, tasks, and run lifecycle.

Status: under construction (epic mc-h4zx)

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

Run `make` to build the worker when its source is available, `make test` to run available test suites, and `make clean` to remove build output. Build artifacts go in `build/`; run artifacts belong in `runs/`. Missing components are added by later implementation beads.

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
  --var problem=/absolute/path/to/my-problem \
  --var instance=n=6 --var notify=<recipient> \
  --var 'overrides=search.mutators=3 stop.duration_s=3600 stop.max_children=100'
```

Omit `instance` and `overrides` to use the problem defaults. The seed must score
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
bead ids and rig/notify context in `run.json`, and registers the engine PID at
`<city>/.gc/funsearch/active/<run-id>.json`. Run ids must be unique across the
city. At completion the finish hook records best versus seed, children scored,
throughput, OK rate, reason and `best.c`, closes slots before the run bead,
sends `gc mail` to `notify`, then removes the registry entry.

The `funsearch-sweep` exec order is dormant when the registry is empty. With
active runs it checks every controller tick but sweeps at most every 30
minutes. A dead engine without a terminal summary is marked failed with
reason `engine died`, using database/backup results where available. A dead
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

The engine needs Python 3.11 or later, a C compiler, and `make`; its Python
modules use only the standard library. It also works without Gas City:

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
```

`run start` checks the seed and returns after the daemon's worker pools are
ready. Configuration, the seed, the problem statement, and the candidate
header are saved in the run directory. Each try or submission compiles a
private source copy; includes of `candidate.h` work there and during rescore.
Try results print `RESULT <status> <score> <message>`; accepted submissions
print `ACCEPTED <program-id> <status> <score>`. A rejected submission leaves
its task open. A compile failure during try consumes a trial and records an
ERROR result. Invalid and crashing submissions are stored so future mutators
can see what was tried. Exact and normalized duplicates are rejected before
scoring; candidates with the same OK score and signature as an active
program are rejected after scoring.

The daemon stops for a requested stop, the duration limit, the submitted-child
limit, or the configured plateau. `children_scored` counts authoritative
submitted evaluations, including behaviour duplicates rejected after scoring;
tries and compile failures do not count. The maximum-child limit includes
in-flight submissions so multiple workers cannot overshoot it. Evaluations
already running can finish for up to two minutes after stopping; queued
requests receive RUN_OVER. Runs export `summary.json`, `best.c`, and the top
ten distinct OK candidates in `top/`. The final and periodic SQLite backups
live in `snapshots/`, with the latest five retained. Worker errors and daemon
tracebacks appear in `engine.log`.

The engine flushes timestamped startup, shutdown, and SIGTERM/SIGHUP/SIGINT
events to `engine.log`; those signals request normal shutdown. Python's
faulthandler writes fatal-signal stack traces there. A small detached parent
waits for the engine and writes `engine-exit.json` with its PID, end time,
exit code, and signal (including SIGKILL). This evidence requires that the
parent survives; a SIGKILL record alone does not establish an OOM cause.

Optional `--on-start 'command'` and `--on-finish 'command'` hooks receive the
absolute run directory as an argument and in `FS_RUN_DIR`. The finish hook
runs after outputs are written, including when the daemon fails. The PID
file is removed after shutdown and the finish hook completes.

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
slot bead. K is `[search] mutators`, the number of slot beads per run.
The agent's `max_active_sessions = 3` supports the default K=3; increase the
rig's agent patch cap for larger K, allowing for other concurrent runs and
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
These adapters are the only engine CLI subcommands that call Gas City.

## Security notes

The mutator guard enforces a Claude tool policy, not process isolation.
Gas City's agent schema has no OS filesystem or network sandbox setting.
Candidate compilation and scoring execute native code with the host user's
access: C includes, compiler options, and candidate code can access files
or the network independently of the agent's file tools. Symlink swaps by
another process remain a race. Do not use this setup as an adversarial
evaluator-hiding boundary; OS/container isolation is separate work.

The pack and generated settings must remain trusted, and a launch override
must not disable project settings/hooks, enable permission bypass, or add
tools/MCP servers. Gas City also supplies its city-managed Claude hooks;
their trusted lifecycle commands are outside the model's tool allowlist.
The later live acceptance bead verifies the actual Claude launch and hook
loading. Policy/helper tests here exercise both allowed and denied calls.
