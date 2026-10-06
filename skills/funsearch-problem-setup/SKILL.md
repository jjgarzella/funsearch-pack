---
name: funsearch-problem-setup
description: Use when setting up a new FunSearch problem, writing an evaluator, or preparing a search run. Provides problem templates, the evaluator C contract, hardening guidance, and validation and launch commands.
---

# Set up a FunSearch problem

## 1. Create the problem directory

A **problem** is a directory containing the mathematical goal, a candidate
interface, a starting candidate, an evaluator, and the configuration for a
search. A **candidate** is a whole source file exporting the functions in
`candidate.h`. An **instance** is an opaque string such as `n=6` or `q=5,d=3`:
there is one per run, and your evaluator receives it unchanged. The evaluator
checks the candidate's output and returns one score, with higher being better.

Set `FS_PACK` to the absolute path of the installed FunSearch pack. Resolve
the skill directory through its symlink if needed; this skill lives at
`$FS_PACK/skills/funsearch-problem-setup`. Copy the templates into the rig where
you want to keep the problem:

```sh
export FS_PACK=/absolute/path/to/funsearch-pack
problem_dir=/absolute/path/to/my-problem
mkdir -p "$problem_dir"
cp -R "$FS_PACK/skills/funsearch-problem-setup/templates/." "$problem_dir/"
```

Check that the directory contains:

```text
my-problem/
  problem.toml                 Configuration
  problem.md                   Goal, known results, and hints
  candidate.h                  Exact candidate prototypes and argument meanings
  seed.c                       A valid starting candidate
  evaluator/evaluator.c        Independent checking and scoring
  evaluator/Makefile           Builds evaluator/libevaluator.so
  runs/                        Created when running; exclude from version control
```

Add `runs/` to the problem's `.gitignore`. The copied evaluator deliberately
returns `FS_ERROR` with `not implemented`; implement it before starting a run.

## 2. Write problem.md

The **mutator**, the agent writing new candidates, reads this file. State the
mathematical goal, valid output constraints, what each instance parameter
means, known constructions or bounds, and useful hints. Explain what a better
score means and what the seed does. Be precise about edge cases, determinism,
and output ownership where they affect the candidate.

Keep private test cases, evaluator source, and evaluator implementation secrets
out of `problem.md`. Describe the public rules sufficiently for a candidate
author to produce valid answers. Use the [prose template](templates/problem.md)
as the starting outline.

## 3. Declare the candidate in candidate.h

Keep the callable surface small. Give exact C prototypes with argument types,
lengths, ranges, return meanings, and any allocation/lifetime rules. The
[header template](templates/candidate.h) declares this illustrative interface:

```c
double priority(const int8_t *v, int32_t n);
```

Here `v` has length `n`, entries in `{0,1,2}`, and is borrowed for the call.
The returned finite number orders vectors; it does not certify their validity.
Replace this interface if your problem needs another one, then update
`seed.c`, `candidate.exports` in `problem.toml`, and symbol resolution in the
evaluator together. The seed must implement the same interface and produce a
valid baseline. Include `candidate.h` in it to catch prototype mismatches.

## 4. Write the evaluator

Include the pack's `include/funsearch.h`; the [Makefile template](templates/evaluator/Makefile)
uses the exported `FS_PACK` variable to add `-I"$(FS_PACK)/include"`.
Build it with `make -C "$problem_dir/evaluator"`. The required and optional
C symbols are:

```c
/* REQUIRED: resolve(name) gives the candidate symbol address, or NULL. */
fs_result fs_score(fs_resolve_fn resolve, const char *instance);
/* OPTIONAL: initialization once before scoring; nonzero means fatal failure. */
int fs_init(const char *instance);
/* OPTIONAL: cleanup at shutdown. */
void fs_fini(void);
```

`fs_result` contains `status`, `double score`, `int32_t nsig`, `double sig[8]`,
and `char msg[256]`. Initialize every field. Use a NUL-terminated message:
it is shown to the candidate author during trials, so do not reveal private
test data. Set `nsig` to `0..8`; optional finite `sig` values describe
**behaviour** for grouping and duplicate rejection, never ranking. Return
one finite score even if your evaluator internally checks several cases.

- `FS_OK`: independently verified valid output; `score` is meaningful.
- `FS_INVALID`: the candidate produced invalid or degenerate output; explain why.
- `FS_ERROR`: evaluation failed, for example due to missing symbols or an
  internal error; explain the failure without claiming success.

The copied [C skeleton](templates/evaluator/evaluator.c) compiles as-is:

```c
#include "funsearch.h"
#include <stdio.h>

fs_result fs_score(fs_resolve_fn resolve, const char *instance)
{
    fs_result result = {0};
    (void)resolve;
    (void)instance;
    result.status = FS_ERROR;
    snprintf(result.msg, sizeof result.msg, "%s", "not implemented");
    return result;
}
```

Replace the body with instance parsing, symbol resolution, candidate calls to
construct the proposed object, independent validation, and scoring. Handle
missing symbols and initialization failures explicitly. Any implementation
language works if the shared library exports these C symbols with this ABI;
for example, a C wrapper can embed Julia. See
[`examples/cap-set`](../../examples/cap-set) for the worked example.

The evaluator shares its worker process with candidate code, so workers start
with an allowlisted environment: `PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`,
`LANGUAGE`, `LC_*`, `TZ`, `TMPDIR` and `LD_LIBRARY_PATH`, plus `FS_MEMORY_MB`.
An evaluator that reads any other variable (an embedded runtime's `JULIA_*`,
your own `MYEVAL_DEBUG`) must list it in `evaluator.env` as an fnmatch pattern,
or `getenv` returns NULL under the engine although it works in your shell.
Candidates can read every variable a worker has, so never pass a token, API
key or licence secret this way: read it from a file only the evaluator needs,
or hard-code non-secret settings. If `fs_init` fails, its stderr (last 2 KiB)
is appended to the startup error, so print the reason before returning nonzero.

Candidate source, including `seed.c`, may not use absolute or `..` includes,
computed includes, `#embed`, inline assembly or `##` token pasting; these
words are rejected even in comments.

## 5. Harden the evaluator before searching

Program search can exploit verifier loopholes. Apply these checks to the
evaluator, including the validation code it calls:

- Independently verify the actual object produced. Never trust a claimed
  score, validity flag, or certificate without checking it. The independent
  check must not call the candidate again.
- Use exact or interval arithmetic wherever rounding could turn an invalid
  result into a valid one. Reject NaN and infinity in candidate outputs,
  scores, and signatures as appropriate.
- Reject degenerate outputs explicitly with `FS_INVALID`: wrong dimensions,
  duplicate entries, empty objects where forbidden, out-of-range values,
  malformed structures, and violated mathematical constraints.
- Bound candidate calls, output sizes, and evaluator work. Set
  `evaluator.timeout_s` and `memory_mb` for the actual workload; a timeout or
  crash is a failure, not a score.
- Never return `FS_OK` after a partial failure, skipped required check,
  arithmetic uncertainty, or internal exception. Use `FS_ERROR` for
  evaluator failures, with a clear message.
- Test a deliberately cheating candidate: fabricate a claimed score, return
  a degenerate object, exploit a boundary or rounding case, or give a
  nonfinite value. Confirm rejection and verify a known good candidate too.

These are the verifier-hardening lessons highlighted by Georgiev,
Gómez-Serrano, Tao, and Wagner in the pack design. Pack implementation details
are documented in [`docs/design.tex`](../../docs/design.tex).

## 6. Configure problem.toml

Read and edit the [annotated template](templates/problem.toml); it includes
all v1 configuration sections. Keep the candidate export list and compilation
commands consistent with your header and source. `{src}` and `{out}` are
substituted source and shared-library paths. `compile_try` adds address and
undefined-behaviour sanitizers for candidate trials; `compile` is the final
scoring build. `evaluator.build` runs from the problem directory; an empty
string uses a prebuilt library instead.

A slung agent may not inherit your shell's exported `FS_PACK`. Before launching
through Gas City, make the build command self-contained by setting
`evaluator.build` to `make -C evaluator FS_PACK=/absolute/path/to/funsearch-pack`
in `problem.toml` (quote the path inside that command if it contains spaces).

Choose these settings deliberately:

| Setting | What to decide |
| --- | --- |
| `problem.instance` | Default instance string, parsed by your evaluator; override with `--instance`. |
| `evaluator.timeout_s` | Time allowed per scoring call, including all required verification. |
| `evaluator.memory_mb` | Memory limit; allow enough for any embedded runtime. |
| `evaluator.env` | Variable patterns the evaluator reads beyond the worker allowlist; never secrets. |
| `search.trial_budget` | Number of local trial evaluations a candidate author can use per task. |
| `search.mutators` | Number of concurrent candidate authors; start small to control cost. |
| `search.tasks_per_session` | Tasks before a candidate author starts with fresh context. |
| `stop.duration_s` | Maximum run duration in seconds. |
| `stop.max_children` | Maximum number of scored submitted children. |
| `stop.plateau_children` | Stop after this many children without improvement; `0` disables this condition. |
| `mutator.model` | Candidate author's model; select a model available in your city. |

Duration and child limits are positive; whichever enabled stop condition is
reached first ends the run. Preserve the other search defaults for an initial
baseline. Run overrides can change the instance and `[search]`/`[stop]` keys.

## 7. Validate before running

With `FS_PACK` exported, run:

```sh
"$FS_PACK/bin/funsearch" check "$problem_dir"
"$FS_PACK/bin/funsearch" check "$problem_dir" --instance 'n=4'
```

`check` validates configuration, builds the evaluator, compiles `seed.c`, and
prints its scored result. It starts no search and exits successfully only
for `FS_OK`. The unimplemented template must report `ERROR` and
`not implemented`; your finished seed must report `OK`.

To test your own known good and deliberately bad candidate files before a
run exists, copy the problem to a scratch directory, replace that copy's
`seed.c` with each candidate in turn, and run `check` on the copy. Expect
`OK` with the known score for the good case and `INVALID` or `ERROR` with a
useful message for the bad case. Keep the original seed intact.

For a candidate already stored in a run, re-evaluate it on another instance:

```sh
"$FS_PACK/bin/funsearch" rescore /absolute/path/to/run best --instance 'n=5'
"$FS_PACK/bin/funsearch" rescore /absolute/path/to/run 17 --instance 'n=5'
```

`rescore` takes a stored program ID or `best`, not a source-file path.

## 8. Start a run and read the results

Import the FunSearch pack into the target rig using your city's pack
configuration, then use the **Running a search** command in the
[pack README](../../README.md#running-a-search). Sling `funsearch-run` to a
general coding-agent pool in that rig, supplying the absolute problem path
and the recipient for completion mail. The README documents this invocation:

```sh
gc sling <rig>/<pool> funsearch-run --formula \
  --var problem=/absolute/path/to/my-problem \
  --var instance=n=6 --var notify=<recipient> \
  --var 'overrides=search.mutators=3 stop.duration_s=3600 stop.max_children=100'
```

Replace the angle-bracket arguments with your rig, pool, and mail recipient.
Omit `instance` and `overrides` to use the problem defaults. Starting a run
requires the seed to score `FS_OK`.

Results appear under `<problem>/runs/<run-id>/`. At completion, `best.c` is
the best candidate and `summary.json` reports best and seed scores, scored
children, success rate, throughput, and the stop reason. The run bead records
the run directory and completion summary; the `notify` recipient receives
completion mail. Inspect progress and request an early stop with:

```sh
"$FS_PACK/bin/funsearch" run status /absolute/path/to/run
"$FS_PACK/bin/funsearch" best /absolute/path/to/run -k 5
"$FS_PACK/bin/funsearch" stop /absolute/path/to/run
```
