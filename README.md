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
mail to `notify`. The run formula and Gas City hooks are supplied by the remaining
implementation beads of epic mc-h4zx.


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

Optional `--on-start 'command'` and `--on-finish 'command'` hooks receive the
absolute run directory as an argument and in `FS_RUN_DIR`. The finish hook
runs after outputs are written, including when the daemon fails. The PID
file is removed after shutdown and the finish hook completes.

CLI exit codes: 0 success; 1 runtime or seed failure; 2 usage/configuration
error; 3 RUN_OVER for next-task/try/submit; 4 rejection or exhausted trial
budget. `run status`, `best`, and `rescore` remain available after completion.
