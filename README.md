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
mail to `notify`. The CLI and run formula are supplied by the remaining
implementation beads of epic mc-h4zx.
