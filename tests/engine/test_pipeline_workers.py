from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import signal
import time
from unittest.mock import patch

from engine.funsearch.compile import compile_candidate, try_worker_env
from engine.funsearch.workers import (
    RECYCLE_FRACTION, RECYCLE_STEP_FRACTION, STDERR_LIMIT_BYTES, EvaluatorInitError, Worker,
    WorkerError, WorkerPool, _checked_reply, worker_binary)
from tests.engine.pipeline_support import PipelineTestCase, ROOT


class WorkerPoolTests(PipelineTestCase):
    def pool(self, size=1, instance="n=1", extra_env=None):
        pool = WorkerPool(self.cfg, self.evaluator(), instance, size, extra_env)
        self.addCleanup(pool.close)
        return pool

    def test_good_negative_missing_symbol_and_load_error(self):
        pool = self.pool(size=2)
        self.assertEqual(pool.score(self.candidate("good"), 5), {
            "status": "OK", "score": 3.5, "sig": [3.5, 1], "msg": "ok"})
        self.assertEqual(pool.score(self.candidate("neg"), 5)["status"], "INVALID")
        self.assertEqual(pool.score(self.candidate("nosym"), 5)["msg"], "missing f")
        self.assertEqual(pool.score(self.root / "absent.so", 5)["status"], "ERROR")
        self.assertEqual(pool.score(self.candidate("good"), 5)["status"], "OK")

    def test_crash_and_recovery(self):
        pool = self.pool()
        process = pool.workers[0].process
        result = pool.score(self.candidate("segv"), 5)
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["msg"], f"worker crashed: signal {signal.SIGSEGV}")
        self.assertIsNot(pool.workers[0].process, process)
        self.assertEqual(process.returncode, -signal.SIGSEGV)
        self.assertEqual(pool.score(self.candidate("good"), 5)["score"], 3.5)

    def test_idle_worker_crash_and_recovery(self):
        pool = self.pool()
        process = pool.workers[0].process
        process.terminate()
        process.wait(timeout=5)
        good = self.candidate("good")
        self.assertEqual(pool.score(good, 5)["msg"], f"worker crashed: signal {signal.SIGTERM}")
        self.assertEqual(pool.score(good, 5)["status"], "OK")

    def test_timeout_and_recovery(self):
        pool = self.pool()
        process = pool.workers[0].process
        result = pool.score(self.candidate("loop"), 1)
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["msg"], "timeout after 1s")
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertEqual(pool.score(self.candidate("good"), 5)["status"], "OK")

    def test_init_failure_is_reported_at_pool_construction(self):
        with self.assertRaisesRegex(EvaluatorInitError, "fs_init returned 1"):
            self.pool(instance="n=1,fail=1")

    def test_exit_three_is_init_error(self):
        binary = self.source("#!/bin/sh\nexit 3\n", "exit-three")
        binary.chmod(0o755)
        with patch("engine.funsearch.workers.worker_binary", return_value=binary):
            with self.assertRaisesRegex(EvaluatorInitError, "code 3"):
                self.pool()

    def test_partial_pool_startup_failure_closes_previous_workers(self):
        counter = self.root / "init-counter"
        mark = self.root / "previous-fini"
        source = self.source('''#include "funsearch.h"
#include <stdio.h>
#include <stdlib.h>
int fs_init(const char *instance) {
    (void)instance;
    FILE *file = fopen(getenv("INIT_COUNTER"), "r");
    int n = 0;
    if (file) { fscanf(file, "%d", &n); fclose(file); }
    file = fopen(getenv("INIT_COUNTER"), "w");
    fprintf(file, "%d", n + 1); fclose(file);
    return n != 0;
}
fs_result fs_score(fs_resolve_fn resolve, const char *instance) {
    (void)resolve; (void)instance;
    return (fs_result){ .status = FS_OK };
}
void fs_fini(void) {
    FILE *file = fopen(getenv("FINI_MARK"), "w");
    if (file) fclose(file);
}
''')
        import shlex
        saved = self.cfg.candidate.exports
        self.cfg.candidate.exports = ["fs_score"]
        self.cfg.candidate.compile += f" -I{shlex.quote(str(ROOT / 'include'))}"
        ok, evaluator, log = compile_candidate(self.cfg, source, self.root / "partial-evaluator", "final")
        self.cfg.candidate.exports = saved
        self.assertTrue(ok, log)
        with self.assertRaisesRegex(EvaluatorInitError, "fs_init returned 1"):
            WorkerPool(self.cfg, evaluator, "", 2,
                       {"INIT_COUNTER": str(counter), "FINI_MARK": str(mark)})
        self.assertEqual(counter.read_text(), "2")
        self.assertTrue(mark.is_file())

    def test_close_reaps_workers_and_calls_fini(self):
        mark = self.root / "fini"
        pool = self.pool(size=2, extra_env={"TOY_FINI_MARK": str(mark)})
        processes = [worker.process for worker in pool.workers]
        pool.close()
        pool.close()
        self.assertTrue(mark.is_file())
        for process in processes:
            self.assertEqual(process.returncode, 0)
            with self.assertRaises(ChildProcessError):
                os.waitpid(process.pid, os.WNOHANG)
            with self.assertRaises(ProcessLookupError):
                os.kill(process.pid, 0)
        with self.assertRaisesRegex(WorkerError, "closed"):
            pool.score(self.candidate("good"), 5)

    def test_normal_and_try_pools(self):
        normal = self.pool()
        sanitizer = self.pool(extra_env=try_worker_env())
        self.assertEqual(normal.workers[0].env["FS_MEMORY_MB"], str(self.cfg.evaluator.memory_mb))
        self.assertNotIn("FS_MEMORY_MB", sanitizer.workers[0].env)
        final = self.candidate("good")
        trial = self.candidate("good", "try")
        self.assertEqual(normal.score(final, 5)["status"], "OK")
        self.assertEqual(sanitizer.score(trial, 5)["score"], 3.5)
        self.assertEqual(sanitizer.score(trial, 5)["score"], 3.5)

    def test_asan_detects_invalid_memory_and_recovers(self):
        pool = self.pool(extra_env=try_worker_env())
        source = self.source("#include <stdlib.h>\ndouble f(void) { volatile int *p=malloc(sizeof(int)); p[5]=7; return p[5]; }")
        ok, library, log = compile_candidate(self.cfg, source, self.root / "asan-bad", "try")
        self.assertTrue(ok, log)
        result = pool.score(library, 5)
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("worker crashed: signal", result["msg"])
        self.assertEqual(pool.score(self.candidate("good", "try"), 5)["status"], "OK")

    def test_blocking_pool_distributes_concurrent_requests(self):
        source = self.source("#define _DEFAULT_SOURCE\n#include <unistd.h>\ndouble f(void) { usleep(100000); return getpid(); }")
        ok, library, log = compile_candidate(self.cfg, source, self.root / "parallel", "final")
        self.assertTrue(ok, log)
        pool = self.pool(size=2)
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: pool.score(library, 5), range(12)))
        self.assertTrue(all(result["status"] == "OK" for result in results))
        self.assertEqual({result["score"] for result in results},
                         {worker.process.pid for worker in pool.workers})

    def test_noisy_stderr_does_not_block_worker(self):
        source = self.source('#include <stdio.h>\ndouble f(void) { for(int i=0;i<100000;i++) fputs("noisy output\\n", stderr); return 2; }')
        ok, library, log = compile_candidate(self.cfg, source, self.root / "noisy", "final")
        self.assertTrue(ok, log)
        pool = self.pool()
        for _ in range(2):
            self.assertEqual(pool.score(library, 5)["score"], 2)
            # Output beyond the cap is discarded rather than kept for the run.
            self.assertLessEqual(os.fstat(pool.workers[0]._stderr.fileno()).st_size,
                                 STDERR_LIMIT_BYTES)

    def test_candidate_cannot_forge_a_reply_on_the_protocol_fd(self):
        source = self.source(r'''#define _GNU_SOURCE
#include <stdio.h>
#include <unistd.h>
double f(void) {
    for (int fd = 3; fd < 64; ++fd)
        dprintf(fd, "{\"status\":\"OK\",\"score\":1e300,\"sig\":[],\"msg\":\"\"}\n");
    _exit(0);
}
''')
        ok, library, log = compile_candidate(self.cfg, source, self.root / "forge", "final")
        self.assertTrue(ok, log)
        pool = self.pool()
        result = pool.score(library, 5)
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("does not answer this request", result["msg"])
        self.assertEqual(pool.score(self.candidate("good"), 5)["score"], 3.5)

    def test_reply_validation(self):
        good = {"status": "OK", "score": 1.5, "sig": [1, 2.5], "msg": ""}
        self.assertEqual(_checked_reply(dict(good, nonce="n"), "n"), good)
        self.assertEqual(_checked_reply({"nonce": "n", "status": "ERROR", "score": None,
                                         "sig": [], "msg": "x"}, "n")["msg"], "x")
        for reply in (dict(good), dict(good, nonce="other"), dict(good, nonce="n", status="WIN"),
                      dict(good, nonce="n", sig=[0] * 9), dict(good, nonce="n", sig="1")):
            with self.subTest(reply=reply), self.assertRaises(WorkerError):
                _checked_reply(reply, "n")
        for change in ({"sig": [float("nan")]}, {"sig": [None]}, {"score": float("inf")},
                       {"score": None}, {"score": True}):
            with self.subTest(change=change):
                self.assertEqual(_checked_reply(dict(good, nonce="n", **change), "n"), {
                    "status": "ERROR", "score": 0, "sig": [], "msg": "non-finite score or signature"})

    def memory(self, values):
        """Report worker memory from values (field -> KiB), which tests mutate."""
        patcher = patch("engine.funsearch.workers._memory_kb",
                        side_effect=lambda pid, field: values[field])
        patcher.start()
        self.addCleanup(patcher.stop)

    def recycle_levels(self, baseline):
        """(threshold, step) in KiB above the baseline for a VmSize worker."""
        budget = self.cfg.evaluator.memory_mb * 1024 - baseline
        return RECYCLE_FRACTION * budget, RECYCLE_STEP_FRACTION * budget

    def test_leaking_worker_is_recycled_before_the_next_request(self):
        baseline = 100_000
        values = {"VmSize": baseline}
        self.memory(values)
        pool = self.pool()
        worker = pool.workers[0]
        process = worker.process
        good = self.candidate("good")
        threshold, step = self.recycle_levels(baseline)
        mark = int(baseline + threshold - 1000)
        # A high but stable level is a retained peak (allocator or GC heap),
        # not a leak: page-level jitter above the threshold never recycles.
        for level in (mark, mark + 2000, mark + 6000, mark + 2000, mark + 6004, mark + 6008):
            values["VmSize"] = level
            self.assertEqual(pool.score(good, 5)["score"], 3.5)
        self.assertIs(worker.process, process)
        # Growth of a step past the mark recycles, but only before the next
        # request: the reply already in hand is returned first.
        values["VmSize"] = int(mark + step)
        self.assertEqual(pool.score(good, 5)["score"], 3.5)
        self.assertIs(worker.process, process)
        values["VmSize"] = baseline + 7
        self.assertEqual(pool.score(good, 5)["score"], 3.5)
        self.assertIsNot(worker.process, process)
        self.assertEqual(process.returncode, 0)
        # The replacement measures its own baseline, not the old reading.
        self.assertEqual(worker._baseline_kb, baseline + 7)

    def test_one_shot_leak_after_start_is_recycled(self):
        # The first score after a (re)start counts: a candidate that leaks
        # past the threshold at once, followed by flat ones, is still replaced.
        baseline = 100_000
        values = {"VmSize": baseline}
        self.memory(values)
        pool = self.pool()
        worker = pool.workers[0]
        process = worker.process
        good = self.candidate("good")
        threshold, _ = self.recycle_levels(baseline)
        values["VmSize"] = int(baseline + threshold + 1)
        self.assertEqual(pool.score(good, 5)["score"], 3.5)
        self.assertIs(worker.process, process)
        self.assertEqual(pool.score(good, 5)["score"], 3.5)
        self.assertIsNot(worker.process, process)

    def test_slow_leak_past_the_threshold_adds_up(self):
        baseline = 100_000
        values = {"VmSize": baseline}
        self.memory(values)
        pool = self.pool()
        worker = pool.workers[0]
        process = worker.process
        good = self.candidate("good")
        threshold, step = self.recycle_levels(baseline)
        mark = int(baseline + threshold)
        for growth in (0, step / 4, step / 2, 3 * step / 4):
            values["VmSize"] = int(mark + growth)
            self.assertEqual(pool.score(good, 5)["score"], 3.5)
        self.assertIs(worker.process, process)
        values["VmSize"] = int(mark + step)
        self.assertEqual(pool.score(good, 5)["score"], 3.5)
        self.assertEqual(pool.score(good, 5)["score"], 3.5)
        self.assertIsNot(worker.process, process)

    def test_growth_up_to_the_threshold_does_not_recycle(self):
        baseline = 100_000
        values = {"VmSize": baseline}
        self.memory(values)
        pool = self.pool()
        worker = pool.workers[0]
        process = worker.process
        good = self.candidate("good")
        threshold, _ = self.recycle_levels(baseline)
        for level in (baseline + threshold - 10, baseline + threshold, baseline):
            values["VmSize"] = level
            self.assertEqual(pool.score(good, 5)["score"], 3.5)
        self.assertIs(worker.process, process)

    def test_sanitizer_worker_recycles_on_resident_size(self):
        # Without FS_MEMORY_MB there is no RLIMIT_AS: only VmRSS is read
        # (VmSize would raise KeyError), and the baseline is not taken off
        # the budget, which has no address-space limit to share with it.
        baseline = 50_000
        values = {"VmRSS": baseline}
        self.memory(values)
        pool = self.pool(extra_env={"FS_MEMORY_MB": None})
        worker = pool.workers[0]
        process = worker.process
        good = self.candidate("good")
        budget = self.cfg.evaluator.memory_mb * 1024
        unreduced = RECYCLE_FRACTION * budget
        reduced = RECYCLE_FRACTION * (budget - baseline)
        # Past the reduced threshold by more than a step, under the real one.
        between = int(baseline + (reduced + unreduced) / 2)
        self.assertGreater(between - baseline - reduced, 0)
        self.assertGreater(between - baseline, RECYCLE_STEP_FRACTION * budget)
        for _ in range(2):
            values["VmRSS"] = between
            self.assertEqual(pool.score(good, 5)["score"], 3.5)
        self.assertIs(worker.process, process)
        values["VmRSS"] = int(baseline + unreduced + RECYCLE_STEP_FRACTION * budget)
        self.assertEqual(pool.score(good, 5)["score"], 3.5)
        values["VmRSS"] = baseline
        self.assertEqual(pool.score(good, 5)["score"], 3.5)
        self.assertIsNot(worker.process, process)

    def test_failed_recycle_fails_later_requests(self):
        values = {"VmSize": 100_000}
        self.memory(values)
        pool = self.pool()
        worker = pool.workers[0]
        good = self.candidate("good")
        values["VmSize"] = self.cfg.evaluator.memory_mb * 1024 - 2
        self.assertEqual(pool.score(good, 5)["score"], 3.5)
        with patch.object(worker, "_start", side_effect=WorkerError("cannot restart")):
            with self.assertRaisesRegex(WorkerError, "cannot restart"):
                pool.score(good, 5)
        with self.assertRaisesRegex(WorkerError, "cannot restart"):
            pool.score(good, 5)

    def test_candidate_environment_is_an_allowlist(self):
        private = {"GC_SESSION_ID": "s", "BEADS_DIR": "/b", "MY_API_TOKEN": "t",
                   "ANTHROPIC_API_KEY": "k", "SSH_AUTH_SOCK": "/agent", "FS_STORE": "/x",
                   "SOLVER_LICENSE": "l"}
        public = {"LC_ALL": "C", "TMPDIR": "/tmp", "SOLVER_LICENSE_FILE": "/opt/l"}
        self.cfg.evaluator.env = ["SOLVER_*_FILE"]
        with patch.dict(os.environ, {**private, **public}):
            pool = self.pool(extra_env={"KEEP_ME": "1"})
        env = pool.workers[0].env
        self.assertFalse(set(private) & set(env))
        self.assertLessEqual(public.items(), env.items())
        self.assertEqual(env["KEEP_ME"], "1")
        self.assertIn("PATH", env)

    def test_startup_failures_include_evaluator_stderr(self):
        init = self.source("#!/bin/sh\necho 'julia: no depot' >&2\nexit 3\n", "init-fail")
        crash = self.source("#!/bin/sh\necho 'cannot dup' >&2\nexit 1\n", "crash")
        for binary, error, text in ((init, EvaluatorInitError, "code 3\nworker stderr:\njulia: no depot"),
                                    (crash, WorkerError, "exit code 1\nworker stderr:\ncannot dup")):
            binary.chmod(0o755)
            with self.subTest(binary=binary.name), \
                    patch("engine.funsearch.workers.worker_binary", return_value=binary):
                with self.assertRaises(error) as caught:
                    self.pool()
                self.assertIn(text, str(caught.exception))

    def test_respawning_is_bounded_to_three_attempts(self):
        pool = self.pool()
        counter = self.root / "starts"
        failing = self.source('#!/bin/sh\necho attempt >> "$START_COUNTER"\nexit 1\n', "fail-worker")
        failing.chmod(0o755)
        worker = pool.workers[0]
        worker.binary = failing
        worker.env["START_COUNTER"] = str(counter)
        with self.assertRaisesRegex(WorkerError, "after 3 attempts"):
            pool.score(self.candidate("segv"), 5)
        self.assertEqual(counter.read_text().splitlines(), ["attempt"] * 3)
        # A permanently failed slot does not launch unbounded replacement loops.
        with self.assertRaisesRegex(WorkerError, "after 3 attempts"):
            pool.score(self.candidate("good"), 5)
        self.assertEqual(counter.read_text().splitlines(), ["attempt"] * 3)

    def test_startup_timeout_is_bounded(self):
        hanging = self.source("#!/bin/sh\nsleep 10\n", "hanging-worker")
        hanging.chmod(0o755)
        with patch("engine.funsearch.workers.worker_binary", return_value=hanging):
            with patch("engine.funsearch.workers.START_TIMEOUT_S", 0.1):
                with self.assertRaisesRegex(WorkerError, "after 3 attempts.*startup timeout"):
                    self.pool()

    def test_timeout_kills_process_group(self):
        marker = self.root / "child-pid"
        source = self.source('''#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
double f(void) {
    if (fork() == 0) {
        FILE *f = fopen(getenv("CHILD_PID_FILE"), "w");
        fprintf(f, "%d", getpid()); fclose(f);
    }
    for (;;) {}
}
''')
        ok, library, log = compile_candidate(self.cfg, source, self.root / "fork", "final")
        self.assertTrue(ok, log)
        pool = self.pool(extra_env={"CHILD_PID_FILE": str(marker)})
        self.assertEqual(pool.score(library, 1)["msg"], "timeout after 1s")
        pid = int(marker.read_text())
        # A killed grandchild may briefly remain a zombie until init reaps it.
        for _ in range(50):
            stat = Path(f"/proc/{pid}/stat")
            if not stat.exists() or stat.read_text().split()[2] == "Z":
                break
            time.sleep(0.01)
        else:
            self.fail(f"worker descendant {pid} survived process-group kill")
        self.assertEqual(pool.score(self.candidate("good"), 5)["status"], "OK")

    def test_invalid_arguments(self):
        for size in (0, -1, True):
            with self.assertRaises(ValueError):
                self.pool(size=size)
        pool = self.pool()
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                pool.score(self.root / "absent.so", timeout)

    def test_missing_worker_is_built_in_this_pack(self):
        # Use a miniature fake pack; never remove another test's live worker.
        root = self.root / "pack with spaces"
        module = root / "engine" / "funsearch" / "workers.py"
        module.parent.mkdir(parents=True)
        (root / "Makefile").write_text("worker:\n\tmkdir -p build\n\ttouch build/funsearch-worker\n")
        with patch("engine.funsearch.workers.__file__", str(module)):
            self.assertEqual(worker_binary(), root / "build" / "funsearch-worker")
            self.assertTrue(worker_binary().is_file())

    def test_stale_worker_is_rebuilt(self):
        root = self.root / "stale pack"
        module = root / "engine" / "funsearch" / "workers.py"
        module.parent.mkdir(parents=True)
        source = root / "worker" / "funsearch-worker.c"
        source.parent.mkdir()
        source.write_text("/* v2 */\n")
        binary = root / "build" / "funsearch-worker"
        binary.parent.mkdir()
        binary.write_text("old")
        os.utime(binary, (1, 1))
        (root / "Makefile").write_text(".PHONY: worker\nworker:\n\techo new > build/funsearch-worker\n")
        with patch("engine.funsearch.workers.__file__", str(module)):
            self.assertEqual(worker_binary(), binary)
        self.assertEqual(binary.read_text(), "new\n")
        # A fresh binary is used as-is, without running make again.
        (root / "Makefile").write_text(".PHONY: worker\nworker:\n\tfalse\n")
        with patch("engine.funsearch.workers.__file__", str(module)):
            self.assertEqual(worker_binary(), binary)
