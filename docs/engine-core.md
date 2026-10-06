# Python engine core

The core requires Python 3.11 or later (for `tomllib`) and uses only the
standard library. From the repository root, imports use `engine.funsearch`;
the `bin/funsearch` entry point adds `engine/` to its module path and imports
`funsearch` directly.

```python
import random
from engine.funsearch.config import load_config
from engine.funsearch.db import Database
from engine.funsearch.evolve import seed_islands, reset_weakest
from engine.funsearch.tasks import create_task

cfg = load_config(problem_dir, ["search.islands=6", "stop.max_children=50"])
with Database(run_dir / "db.sqlite") as db:
    # The compile/evaluation layer must verify the seed before this call.
    seed_islands(db, cfg, source, score=result["score"],
                 sig=result["sig"], msg=result["msg"])
    rng = random.Random(42)
    task_id = create_task(db, cfg, problem_dir, run_dir, "slot-1", rng=rng)
```

`load_config` validates TOML, typed overrides, positive limits (with
`stop.plateau_children=0` disabling the plateau limit), nonempty exports, and
required input files. Instance strings remain opaque. Evaluator build output
need not exist yet. Every schema field has a dataclass default; `exports` must be
supplied. Problem name and instance default to empty strings when omitted.

`Database` opens SQLite with DELETE rollback journaling, foreign keys, and a
5000 ms busy timeout. Writable opens migrate existing WAL databases. Read-only
clients use `readonly=True` and a `mode=ro` URI without schema changes.
Program, Task, Trial, and Evaluation accessors return immutable dataclass records;
JSON fields are decoded to Python values. Each process should open its own
connection. `transaction()` supports composing accessor writes atomically through
nested savepoints. `backup(path)` uses the SQLite backup API.

The primary operations are:

- Programs: `add_program`, `get_program`, `list_programs`, `best_program`,
  `has_normalized_hash`, `recent_children`, `archive_island`. Hot paths filter
  and rank in SQL: `best_program` and the lazy `ranked_ids` order scored `OK`
  programs by score, then shorter source, then id. A stored `source_length`
  column and two rank-ordered covering indexes let ranking stop after its
  prefix without sorting or reading source text; `scored_summaries` and
  `count_programs` give sampling its fields from the same indexes;
  `has_scored_duplicate(score, sig)` is the submission duplicate check.
  Writable opens add `source_length` to older databases; read-only opens of an
  unmigrated database see it through a temporary view.
- Tasks: `add_task`, `get_task`, `list_tasks`, `close_task`;
  `open_tasks_for_slot(slot)` lists a slot's unfinished tasks (more than one
  means the slot is corrupt); `abandon_stale_tasks(cutoff)` abandons open tasks
  created before `cutoff` that have no queued or running evaluation, and returns
  how many it closed. Empty abandonment sweeps take no writer lock; the daemon
  runs them at most once every 30 seconds.
- Trials: `reserve_trial(task_id, budget)` atomically consumes a budget slot;
  `add_trial` stores its eventual result; `list_trials` reads results in order.
- Queue: `enqueue`, `claim_evaluation` (oldest queued item, atomically),
  `finish_evaluation`, `get_evaluation`; `has_pending_submission(task_id)` is
  true while a submission for the task is queued or running, and
  `running_evaluations()` lists running items. Queue items transition queued →
  running → done. Empty queue polls take no writer lock; a nonempty claim
  reselects under the writer transaction to remain atomic. If the engine dies, `funsearch run recover <run-dir>`
  (`daemon.recover_outputs`, which the Gas City sweep calls) finishes
  any request the dead engine still held as an error and writes failed outputs
  from the live database, or from the newest readable snapshot when the live
  one is unreadable.
- State: `set_state`, `get_state`, `increment_state`, `all_state` (every key);
  values are JSON.

Code outside the engine (the Gas City lifecycle script, the mutator guard)
uses these methods rather than SQL, so the schema stays private to `db.py`.

The core owns state keys `islands`, `next_island`, and `island_resets`. Later
layers can add run status, started_at, best score, and counters with these state
accessors. Task ids and program ids are integers. Task directories are
`<run-dir>/tasks/<task-id>/`; `create_task` returns the id after writing TASK.md.
It rolls back database changes and removes newly created task files on an error.
Existing directories are preserved and cause an error rather than being reused.

Normalization hashes canonical C tokens with SHA-256. It ignores comments
(including IDEA lines), preserves string and character literals, and preserves
preprocessor line boundaries and object/function macro distinctions. IDEA
extraction skips literal contents and returns the first actual `// IDEA:` comment.
It does not attempt general C semantic equivalence.

Evolution groups OK programs by exact score and signatures rounded to eight
decimal places. Cluster selection uses range-normalized scores and Boltzmann
weights with `T0=0.1`, `period=30000`, and
`T=T0*(1-(n_island % period)/period)`. Member selection uses range-normalized
negative source length at temperature 1. Parents are sampled without replacement;
invalid programs never become parents. All stochastic evolution accepts an
explicit `random.Random` for reproducible decisions.

`reset_weakest` archives the weaker floor(N/2) islands, with random tie breaking,
and copies each new seed from the best program of a randomly selected survivor.
Archived programs retain their ids so open tasks can still read their parents.
`list_programs` and hash checks default to active programs; `recent_children`
includes archived history for the mutator's last-ten summary. New reset seeds
have no parents and therefore are excluded from that summary.

Run the core acceptance tests with:

```sh
python3 -m unittest discover -s tests -t . -p 'test_core_*.py' -v
make test
```
