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
