# Cap-set example

This problem demonstrates the full evaluator chain: a C worker calls a C
evaluator that embeds Julia, and Julia calls the candidate's C `priority`
function through `ccall`. Julia starts once per worker and scores subsequent
candidates in the same runtime. Each cap is verified with an independent exact
checker before its size is accepted.

Prerequisites: a C compiler, make, Python 3.11+, and Julia on `PATH` (tested
with Julia 1.13). Install Julia or add your Julia installation's `bin` directory
to `PATH`. The evaluator Makefile queries Julia for its include and library
directories and embeds the library rpath; it contains no installation paths.
Use `make -C examples/cap-set/evaluator JULIA=/path/to/julia` to override the
build executable. A copied evaluator can use `FS_PACK=/path/to/funsearch-pack`
to locate the pack's C header.

From the pack root, run the pre-flight:

```sh
make worker
bin/funsearch check examples/cap-set
```

The configuration uses `n=6`, a 30-second scoring timeout, 4096 MB per worker,
four islands, three mutators, two workers, and a one-hour/100-child stopping
limit. The instance accepts dimensions 1 through 8; enumeration is exponential.
The constant seed scores 64 at n=6, with signature `[16, 32, 64]`. For n>=4
the signature contains sizes at dimensions 4 through min(n,6), followed by the
requested dimension if n>6. For n<4 it contains dimensions 1 through n.
Equal priorities are ordered lexicographically. Non-finite priorities and
input mutations are INVALID; missing symbols and evaluator failures are ERROR.
Candidates that crash terminate their worker and are handled by the engine.

ASan trial workers need a BLAS loader adjustment during Julia startup. When
the evaluator detects a preloaded ASan runtime, it sets
`LBT_USE_RTLD_DEEPBIND=0` before `jl_init`, using
[libblastrampoline's sanitizer support](https://github.com/JuliaLinearAlgebra/libblastrampoline/blob/main/src/dl_utils.c).
The cap construction uses no BLAS. The engine removes the address-space limit
for sanitizer trials because ASan reserves a large shadow mapping; final
scoring retains the configured 4096 MB limit.

To save and independently check every verified cap during a run, set the
dump directory before starting it (the engine and workers must inherit it;
`evaluator.env` in problem.toml passes `FS_CAPSET_*` and `JULIA_*` through the
workers' allowlisted environment):

```sh
export FS_CAPSET_DUMP="$PWD/build/cap-dumps"
bin/funsearch check examples/cap-set
python3 tools/check_cap.py "$FS_CAPSET_DUMP"
```

Dump filenames end with `-n<dimension>.txt` and begin with a UTC timestamp,
plus process and nanosecond identifiers. Each line contains one vector as
space-separated integers. A new file is written for every verified dimension
of every score; the checker rejects duplicate vectors, malformed coordinates,
and any line in the set. An empty dump directory also fails checking.

Run the full acceptance suite from the pack root:

```sh
make worker && make -C examples/cap-set/evaluator && make test
```

The example suite runs the worker directly, so it also works before the CLI is
installed. It verifies the seed, a stronger candidate, NaN/Inf rejection,
input mutation rejection, missing exports, an expected crash, and independent
Python re-checks of the dumped caps. It also checks Julia startup failures and
ASan trial-worker initialization. `FS_CAPSET_TIMINGS=1` reports `fs_init`
and each `fs_score` duration to stderr. The first score includes Julia JIT
compilation; later scores use the warm embedded runtime. Enabling dumps adds
file I/O to scoring timings.
