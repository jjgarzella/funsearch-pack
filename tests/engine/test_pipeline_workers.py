from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import signal
import time
from unittest.mock import patch

from engine.funsearch.compile import compile_candidate, try_worker_env
from engine.funsearch.workers import EvaluatorInitError, Worker, WorkerError, WorkerPool, worker_binary
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
        self.assertEqual(self.pool().score(library, 5)["score"], 2)

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
