from concurrent.futures import ThreadPoolExecutor
import copy
import os
from pathlib import Path
import random
import signal
import subprocess
import time
import unittest
from unittest.mock import patch

from engine.funsearch.compile import compile_candidate, try_worker_env
from engine.funsearch.workers import (
    RECYCLE_SCORES, STDERR_LIMIT_BYTES, STDERR_TAIL_BYTES, EvaluatorInitError, Worker,
    WorkerError, WorkerPool, _StderrTail, _checked_reply, worker_binary)
from tests.engine.pipeline_support import PipelineTestCase, ROOT


class StderrTailTests(unittest.TestCase):
    def test_close_stops_reader_even_when_writer_keeps_pipe_open(self):
        read_fd, write_fd = os.pipe()
        reader = _StderrTail(os.fdopen(read_fd, "rb", buffering=0))
        self.addCleanup(reader.close)
        self.addCleanup(os.close, write_fd)
        os.write(write_fd, b"last diagnostic\n")
        # An escaped descendant can retain the write end; close must not
        # require that descendant to cooperate or wait for EOF.
        started = time.monotonic()
        reader.close()
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(reader.thread.is_alive())
        self.assertEqual(reader.tail(), "last diagnostic")


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
        self.assertTrue(result["msg"].startswith(f"worker crashed: signal {signal.SIGSEGV}"))
        self.assertIn("toy evaluator", result["msg"])
        self.assertIsNot(pool.workers[0].process, process)
        self.assertEqual(process.returncode, -signal.SIGSEGV)
        self.assertEqual(pool.score(self.candidate("good"), 5)["score"], 3.5)

    def test_idle_worker_crash_and_recovery(self):
        pool = self.pool()
        process = pool.workers[0].process
        process.terminate()
        process.wait(timeout=5)
        good = self.candidate("good")
        # The dead worker's EOF is noticed before the request is sent, so the
        # next candidate is scored by a replacement instead of being charged.
        self.assertEqual(pool.score(good, 5)["status"], "OK")
        self.assertIsNot(pool.workers[0].process, process)

    def test_timeout_and_recovery(self):
        pool = self.pool()
        process = pool.workers[0].process
        result = pool.score(self.candidate("loop"), 1)
        self.assertEqual(result["status"], "ERROR")
        self.assertTrue(result["msg"].startswith("timeout after 1s"))
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
        sanitizer = self.pool(extra_env=try_worker_env(self.cfg))
        self.assertEqual(normal.workers[0].env["FS_MEMORY_MB"], str(self.cfg.evaluator.memory_mb))
        self.assertNotIn("FS_MEMORY_MB", sanitizer.workers[0].env)
        final = self.candidate("good")
        trial = self.candidate("good", "try")
        self.assertEqual(normal.score(final, 5)["status"], "OK")
        self.assertEqual(sanitizer.score(trial, 5)["score"], 3.5)
        self.assertEqual(sanitizer.score(trial, 5)["score"], 3.5)

    def test_asan_detects_invalid_memory_and_recovers(self):
        pool = self.pool(extra_env=try_worker_env(self.cfg))
        source = self.source("#include <stdlib.h>\ndouble f(void) { volatile int *p=malloc(sizeof(int)); p[5]=7; return p[5]; }")
        ok, library, log = compile_candidate(self.cfg, source, self.root / "asan-bad", "try")
        self.assertTrue(ok, log)
        result = pool.score(library, 5)
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("worker crashed: signal", result["msg"])
        self.assertEqual(pool.score(self.candidate("good", "try"), 5)["status"], "OK")

    def test_try_worker_memory_is_bounded_without_rlimit_as(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.evaluator.memory_mb = 256
        pool = WorkerPool(cfg, self.evaluator(), "n=1", 1, try_worker_env(cfg))
        self.addCleanup(pool.close)
        source = self.source("#include <stdlib.h>\n#include <string.h>\n"
                             "double f(void) { for (;;) { char *p = malloc(32 << 20);"
                             " if (!p) return -1; memset(p, 1, 32 << 20); } }")
        ok, library, log = compile_candidate(cfg, source, self.root / "hog", "try")
        self.assertTrue(ok, log)
        result = pool.score(library, 20)
        self.assertEqual(result["status"], "ERROR")
        # ASan aborts at hard_rss_limit_mb rather than letting RSS grow.
        self.assertTrue(result["msg"].startswith(f"worker crashed: signal {signal.SIGABRT}"),
                        result["msg"][:200])
        self.assertEqual(pool.score(self.candidate("good", "try"), 5)["status"], "OK")

    def test_oversized_reply_without_newline_fails_promptly(self):
        source = self.source(r'''#include <string.h>
#include <unistd.h>
double f(void) {
    static char chunk[4096];
    memset(chunk, 'x', sizeof chunk);
    for (int fd = 3; fd < 64; ++fd)
        for (int i = 0; i < 32; ++i)
            if (write(fd, chunk, sizeof chunk) < 0)
                break;
    sleep(30);
    return 0;
}
''')
        ok, library, log = compile_candidate(self.cfg, source, self.root / "flood", "final")
        self.assertTrue(ok, log)
        pool = self.pool()
        process = pool.workers[0].process
        started = time.monotonic()
        result = pool.score(library, 20)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(result["status"], "ERROR")
        self.assertTrue(result["msg"].startswith(
            "worker protocol error: worker response exceeds 64 KiB"), result["msg"])
        self.assertIsNot(pool.workers[0].process, process)
        self.assertEqual(pool.score(self.candidate("good"), 5)["score"], 3.5)

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
            self.assertLessEqual(len(pool.workers[0]._stderr.buffer), STDERR_LIMIT_BYTES)

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

    def test_forged_reply_with_the_request_token_is_detected(self):
        # The token is not in any request buffer, but a candidate sharing the
        # process can still find the worker's copy. The SYNC barrier then sees
        # the worker's own reply as a second line.
        source = self.source(r"""#define _GNU_SOURCE
#include <link.h>
#include <stdio.h>
#include <string.h>
static char token[33];
static int scan(struct dl_phdr_info *info, size_t size, void *data) {
    (void)size; (void)data;
    if (info->dlpi_name[0]) return 0;
    for (int i = 0; i < info->dlpi_phnum; ++i) {
        const ElfW(Phdr) *ph = &info->dlpi_phdr[i];
        if (ph->p_type != PT_LOAD || !(ph->p_flags & PF_W)) continue;
        const char *p = (const char *)(info->dlpi_addr + ph->p_vaddr);
        for (size_t j = 1; j + 33 <= ph->p_memsz; ++j)
            if (!p[j - 1] && !p[j + 32] && strspn(p + j, "0123456789abcdef") == 32) {
                memcpy(token, p + j, 32);
                return 1;
            }
    }
    return 0;
}
double f(void) {
    if (!dl_iterate_phdr(scan, NULL)) return -1;
    for (int fd = 3; fd < 64; ++fd)
        dprintf(fd, "{\"nonce\":\"%s\",\"status\":\"OK\",\"score\":1e300,\"sig\":[],\"msg\":\"\"}\n", token);
    return 0;
}
""")
        ok, library, log = compile_candidate(self.cfg, source, self.root / "forge-token", "final")
        self.assertTrue(ok, log)
        pool = self.pool()
        process = pool.workers[0].process
        result = pool.score(library, 5)
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("extra output on the protocol fd", result["msg"])
        self.assertIsNot(pool.workers[0].process, process)
        self.assertEqual(pool.score(self.candidate("good"), 5)["score"], 3.5)

    def test_late_protocol_output_is_not_charged_to_the_next_request(self):
        source = self.source(r"""#define _DEFAULT_SOURCE
#include <stdio.h>
#include <unistd.h>
double f(void) {
    if (fork() == 0) {
        usleep(200000);
        dprintf(3, "{\"status\":\"OK\",\"score\":1e300,\"sig\":[],\"msg\":\"\"}\n");
        _exit(0);
    }
    return 1;
}
""")
        ok, library, log = compile_candidate(self.cfg, source, self.root / "late", "final")
        self.assertTrue(ok, log)
        pool = self.pool()
        process = pool.workers[0].process
        self.assertEqual(pool.score(library, 5)["score"], 1)
        time.sleep(0.6)
        self.assertEqual(pool.score(self.candidate("good"), 5)["score"], 3.5)
        self.assertIsNot(pool.workers[0].process, process)

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

    def test_fixed_score_budget_recycles_before_next_request(self):
        self.assertEqual(RECYCLE_SCORES, 100)
        pool = self.pool()
        worker = pool.workers[0]
        good = self.candidate("good")
        for _ in range(2):
            process = worker.process
            reader = worker._stderr
            for n in range(worker._scores, RECYCLE_SCORES):
                self.assertEqual(pool.score(good, 5)["score"], 3.5)
                self.assertIs(worker.process, process)
                self.assertEqual(worker._scores, n + 1)
            # Recycling must not delay returning score 100 or affect its value.
            self.assertTrue(worker._recycle_due)
            self.assertEqual(pool.score(good, 5)["score"], 3.5)
            self.assertIsNot(worker.process, process)
            self.assertEqual(process.returncode, 0)
            self.assertFalse(reader.thread.is_alive())
            self.assertEqual(worker._scores, 1)

    def test_sanitizer_uses_same_fixed_budget(self):
        pool = self.pool(extra_env=try_worker_env(self.cfg))
        worker = pool.workers[0]
        good = self.candidate("good", "try")
        process = worker.process
        with patch("engine.funsearch.workers.RECYCLE_SCORES", 3):
            for _ in range(3):
                self.assertEqual(pool.score(good, 5)["score"], 3.5)
                self.assertIs(worker.process, process)
            self.assertEqual(pool.score(good, 5)["score"], 3.5)
        self.assertIsNot(worker.process, process)
        self.assertEqual(worker._scores, 1)

    def test_invalid_and_error_replies_count_toward_recycling(self):
        pool = self.pool()
        worker = pool.workers[0]
        process = worker.process
        with patch("engine.funsearch.workers.RECYCLE_SCORES", 3):
            for library, status in ((self.candidate("good"), "OK"),
                                    (self.candidate("neg"), "INVALID"),
                                    (self.root / "absent.so", "ERROR")):
                self.assertEqual(pool.score(library, 5)["status"], status)
                self.assertIs(worker.process, process)
            self.assertEqual(pool.score(self.candidate("good"), 5)["status"], "OK")
        self.assertIsNot(worker.process, process)

    def test_failed_recycle_preserves_last_reply_and_fails_later_requests(self):
        pool = self.pool()
        worker = pool.workers[0]
        good = self.candidate("good")
        with patch("engine.funsearch.workers.RECYCLE_SCORES", 1), \
                patch.object(worker, "_start", side_effect=WorkerError("cannot restart")) as start:
            self.assertEqual(pool.score(good, 5)["score"], 3.5)
            start.assert_not_called()
            for _ in range(2):
                with self.assertRaisesRegex(WorkerError, "cannot restart"):
                    pool.score(good, 5)
            self.assertEqual(start.call_count, 1)

    def test_stderr_is_bounded_during_scoring_and_crash_tail_survives(self):
        source = self.source(r'''#include <stdio.h>
#include <unistd.h>
double f(void) {
    for (int i=0; i<300000; ++i) fputs("discard this native output\n", stderr);
    fputs("LAST-DIAGNOSTIC-MARKER\n", stderr);
    sleep(1);
    _exit(1);
}
''')
        ok, library, log = compile_candidate(self.cfg, source, self.root / "crash-tail", "final")
        self.assertTrue(ok, log)
        pool = self.pool()
        reader = pool.workers[0]._stderr
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(pool.score, library, 10)
            deadline = time.monotonic() + 5
            while "LAST-DIAGNOSTIC-MARKER" not in reader.tail():
                self.assertFalse(future.done())
                self.assertLess(time.monotonic(), deadline)
                self.assertLessEqual(len(reader.buffer), STDERR_LIMIT_BYTES)
                time.sleep(0.01)
            self.assertFalse(future.done())
            result = future.result(timeout=10)
        self.assertEqual(result["status"], "ERROR")
        self.assertTrue(result["msg"].startswith("worker crashed: exit code 1"))
        self.assertIn("LAST-DIAGNOSTIC-MARKER", result["msg"])
        self.assertLessEqual(len(result["msg"].encode()), STDERR_TAIL_BYTES + 100)
        self.assertFalse(reader.thread.is_alive())
        self.assertEqual(pool.score(self.candidate("good"), 5)["score"], 3.5)

    def test_nonfinite_evaluator_signature_does_not_poison_sampling(self):
        import shlex
        from engine.funsearch.db import Database
        from engine.funsearch.evolve import sample_parents
        self.cfg.evaluator.build = ("mkdir -p evaluator && cc -shared -fPIC "
            f"-I{shlex.quote(str(ROOT / 'include'))} -o evaluator/libevaluator.so "
            f"{shlex.quote(str(ROOT / 'tests' / 'worker' / 'edge_eval.c'))}")
        pool = self.pool(instance="nan-sig")
        result = pool.score(self.candidate("good"), 5)
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["sig"], [])
        self.assertIn("non-finite signature", result["msg"])
        with Database(self.root / "db.sqlite") as db:
            db.add_program(0, "double f(void){return 0;}", score=0, sig=[0])
            db.add_program(0, "double f(void){return 1;}", **result)
            self.assertTrue(sample_parents(db, 0, 1, random.Random(1)))

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
        self.assertTrue(pool.score(library, 1)["msg"].startswith("timeout after 1s"))
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

    def test_concurrent_process_builds_are_serial_and_atomic(self):
        import shutil
        root = self.root / "atomic pack"
        root.mkdir()
        shutil.copy(ROOT / "Makefile", root / "Makefile")
        for name in ("worker/funsearch-worker.c", "include/funsearch.h"):
            path = root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("source")
        compiler = root / "fake-cc"
        compiler.write_text("#!/bin/sh\necho build >> builds\nwhile [ \"$1\" != -o ]; do shift; done\nshift\nprintf partial > \"$1\"\nsleep 0.3\nprintf complete > \"$1\"\n")
        compiler.chmod(0o755)
        binary = root / "build" / "funsearch-worker"
        binary.parent.mkdir()
        binary.write_text("old")
        os.utime(binary, (1, 1))
        processes = [subprocess.Popen(["make", "worker", "CC=./fake-cc"], cwd=root,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
        observations = set()
        try:
            while any(p.poll() is None for p in processes):
                observations.add(binary.read_text())
                time.sleep(0.01)
        finally:
            for process in processes:
                output, errors = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, output + errors)
        self.assertLessEqual(observations, {"old", "complete"})
        self.assertEqual(binary.read_text(), "complete")
        self.assertEqual((root / "builds").read_text().splitlines(), ["build"])
