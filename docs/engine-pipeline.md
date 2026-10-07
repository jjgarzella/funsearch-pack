# Compile and evaluator pipeline

The pipeline requires Python's standard library, a C compiler, `make`, `flock`, and a
POSIX host with process groups and `select`. Import from `engine.funsearch` at
the pack root, or add `engine/` to the module path and import `funsearch`.

```python
from concurrent.futures import ThreadPoolExecutor
from engine.funsearch.config import load_config
from engine.funsearch.compile import compile_candidate, try_worker_env
from engine.funsearch.evaluator import build_evaluator
from engine.funsearch.workers import WorkerPool

cfg = load_config(problem_dir)
evaluator = build_evaluator(cfg, problem_dir)
with WorkerPool(cfg, evaluator, cfg.problem.instance, cfg.search.workers) as final_pool:
    with WorkerPool(cfg, evaluator, cfg.problem.instance, cfg.search.workers,
                    extra_env=try_worker_env(cfg)) as try_pool:
        ok, library, log = compile_candidate(cfg, candidate_c, trial_dir, "try")
        if ok:
            result = try_pool.score(library, cfg.evaluator.timeout_s)
        # Several caller threads can share a pool. Each call blocks until an
        # idle worker is available, then returns its result dictionary.
        with ThreadPoolExecutor(max_workers=cfg.search.workers) as executor:
            pending = executor.submit(try_pool.score, library, cfg.evaluator.timeout_s)
```

`compile_candidate(cfg, source, out_dir, mode)` accepts `try` and `final`, uses
the corresponding configured shell command, and replaces `{src}` and `{out}`
with shell-quoted absolute paths. It returns `(ok, library_path, log)`. Each
concurrent compilation must use its own output directory. The output is
`candidate.so`; an existing output is removed before compilation, and failed
outputs are removed too. Commands run in the output directory for at most 60
seconds, and stderr logs are truncated to 4 KiB. Timeout kills the compiler's
whole process group.

Every required export must be defined in `nm -D --defined-only` output. Undefined
references do not satisfy this check. When `nm` is unavailable, a separate
Python process loads the library with `ctypes.CDLL` and checks `hasattr`; a
constructor crash cannot take down the engine. For try builds that fallback
uses the ASan environment too. An installed but failing `nm` reports an error.

`build_evaluator` runs the configured nonempty build command in the problem
directory with a 15 minute timeout, verifies the library exists, sits in a
directory that does not contain the problem's `runs/`, and defines `fs_score`,
and returns its absolute path. Failures raise `EvaluatorBuildError`.
`snapshot_evaluator(library, directory)` copies the library's directory to
`directory/evaluator` (symlinks as links) and returns the copied library;
`evaluator_digest(directory)` hashes every file's content and every symlink's
target in such a snapshot. `run start` scores the seed with a staged snapshot
and moves it into the run.
An empty build command supports prebuilt evaluators. The toy fixture's build
command references the evaluator and header shipped elsewhere in this repo.

`worker_binary()` locates the pack relative to this module and invokes
`make -C <pack> worker` if `build/funsearch-worker` is missing or older than
the worker source or `funsearch.h`. The build holds an flock on
`build/.build.lock` across processes. Direct Makefile builds also serialize
on `build/.worker-build.lock` and install from a unique temporary path by
atomic rename, so concurrent engines never execute a partial binary. A current binary
needs neither the lock nor a writable pack. `Worker` and
`WorkerPool` both support context managers and idempotent `close()`.

A worker starts in a new process group with `FS_MEMORY_MB` from configuration.
The engine sends a blank request and reads its `bad request` response to ensure
`fs_init` has finished; the C protocol has no spontaneous ready response.
A fatal response or exit code 3 raises `EvaluatorInitError`. Other startup
failures retry at most three times, with a 60 second limit per attempt. A failed
replacement remains failed and raises on later calls instead of retrying forever.
Worker stderr is continuously drained by a reader thread, retaining only its
last 1 MiB in memory throughout initialization and scoring. Startup failures,
crashes, protocol failures and timeouts append the last 2 KiB to the diagnostic.
Native output is untrusted diagnostic text within the v1 trusted-local model.
The reader closes with the process, and no stderr temporary file accumulates.
Workers get `worker_environment(cfg)`: the `CANDIDATE_ENV` allowlist, plus
variables matching `evaluator.env` and `FS_MEMORY_MB`. The export check's
ctypes fallback loads both candidate and evaluator libraries under the same
environment (with the sanitizer overlay for try-mode candidates), so library
constructors see what the worker will.

Each request is `SCORE #<token> <path>` with a fresh random token. A reply
counts only if it echoes that token; anything else is a protocol error, scored
ERROR, and the worker is replaced. The worker reads requests with `read(2)`
into a private buffer and wipes it before candidate code runs. After accepting
a reply, the engine sends `SYNC #<token2>` and requires the next line to be
`{"sync":"<token2>"}`; a second reply first (candidate code wrote one carrying
the token) is a protocol error, scored ERROR, and the worker is replaced. The
barrier waits until the scoring deadline, or at least `SYNC_TIMEOUT_S` (5 s)
after the reply. Before each request, any protocol output that arrived after
the last barrier, or a dead worker's EOF, replaces the worker outside the
deadline instead of being read as the next candidate's reply. Candidates share
the worker's address space, so this raises the bar without being a boundary.
Replies must also carry a finite score (or null for non-OK statuses) and at
most eight finite signature values; otherwise the result becomes ERROR.

A warm worker accumulates whatever earlier candidates retained. The engine
recycles it after 100 completed scoring replies (OK, INVALID or ERROR), at the
start of the next request, before that request's timeout begins. This fixed
budget applies to both normal and sanitizer workers on every host. It avoids
restarts based on noisy resident/virtual memory measurements or GC high-water
marks, preserves the result already in hand, and bounds the lifetime of leaked
state. It does not guarantee that a single candidate or a burst of leaks cannot
exhaust memory before the budget; crashes and timeouts still replace workers.
A replacement starts its own fresh 100-score budget. If replacement fails,
subsequent calls fail without repeatedly attempting startup.

`score(library, timeout_s)` returns `status`, `score`, `sig`, and `msg`. Scoring
timeouts return ERROR with `timeout after Ns`; crashes return ERROR with
`worker crashed: signal N` or `worker crashed: exit code N`. Both kill the old
process group and start a replacement before returning. A failure to start that
replacement raises `WorkerError` or `EvaluatorInitError`. The timeout starts
once a worker becomes available; time waiting for an idle worker and restarting
a process is additional. `start_budget_s()` and `score_budget_s(timeout_s)`
bound a worker start and a whole `score` call (a recycle before the candidate,
the SYNC barrier, and a replacement after it); the daemon publishes its client deadlines from
them as the `claim_timeout_s` and `end_by` state keys, which waiting `try` and
`submit` clients compare against. `claim_timeout_s` is a duration in seconds
(`score_budget_s(timeout_s)` plus 60 s of slack), measured from a claimed
evaluation's `started_at`. `end_by` is an absolute `time.time()` epoch:
`started_at` plus `stop.duration_s`, the stop grace and `claim_timeout_s`. The
daemon writes both in the transaction that sets status `running`, so a running
run always has them; a client that finds them missing reports that the engine
predates it. `close()` rejects new/waiting pool requests, waits for
active requests, sends QUIT, and allows one second for shutdown before killing
and reaping each process and cleaning up its pipes and stderr reader.

Try workers need the ASan runtime before loading sanitized candidate libraries.
`try_worker_env(cfg)` asks `${CC:-cc} -print-file-name=libasan.so` for that runtime,
prepends it to `LD_PRELOAD`, and sets `ASAN_OPTIONS` to
`detect_leaks=0:abort_on_error=1:hard_rss_limit_mb=M:max_allocation_size_mb=M:allocator_may_return_null=1`
with `M = evaluator.memory_mb`. It also removes `FS_MEMORY_MB`:
ASan reserves a large virtual shadow address range, so the normal `RLIMIT_AS`
limit makes sanitizer workers abort with `Failed to mmap` before evaluation.
The normal pool retains the configured address-space limit. Sanitizer trials
keep the same bound in ASan's terms instead: a worker whose resident memory
exceeds `memory_mb` aborts (an `ERROR` crash result and a replacement worker),
and a single larger allocation returns NULL. The
environment overlay accepts `None` to remove a variable explicitly.

Run the pipeline acceptance checks from the pack root:

```sh
make worker
python3 -m unittest discover -s tests -t . -p 'test_pipeline_*.py' -v
make test
```
