from concurrent.futures import ThreadPoolExecutor
import copy
import errno
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from engine.funsearch._process import kill_group
from engine.funsearch.compile import compile_candidate, try_worker_env
from engine.funsearch.workers import (
    RECYCLE_SCORES, STDERR_LIMIT_BYTES, STDERR_TAIL_BYTES, EvaluatorInitError, Worker,
    WorkerError, WorkerPool, _StderrTail, _checked_reply, worker_binary)
from tests.engine.pipeline_support import PipelineTestCase, ROOT


class ProcessGroupTests(unittest.TestCase):
    def test_permission_error_reaps_worker_and_reports_possible_descendants(self):
        for leader_status, should_kill in ((None, True), (-signal.SIGKILL, False)):
            with self.subTest(leader_status=leader_status):
                process = Mock(pid=123, poll=Mock(return_value=leader_status))
                with patch("engine.funsearch._process.sys.platform", "linux"), \
                        patch("engine.funsearch._process.os.killpg",
                              side_effect=PermissionError("permission denied")) as killpg:
                    with self.assertRaisesRegex(
                            PermissionError,
                            r"could not signal process group 123; descendants may remain"):
                        kill_group(process)

                killpg.assert_called_once_with(123, signal.SIGKILL)
                process.poll.assert_called_once_with()
                if should_kill:
                    process.kill.assert_called_once_with()
                else:
                    process.kill.assert_not_called()
                process.wait.assert_called_once_with()

    def test_darwin_permission_error_for_zombie_only_group_is_ignored(self):
        process = Mock(pid=123, poll=Mock(return_value=-signal.SIGKILL))
        with patch("engine.funsearch._process.sys.platform", "darwin"), \
                patch("engine.funsearch._process.os.killpg",
                      side_effect=PermissionError("permission denied")) as killpg, \
                patch("engine.funsearch._process._darwin_group_has_live_members",
                      return_value=False) as has_live_members:
            kill_group(process)

        self.assertEqual(killpg.call_count, 2)
        self.assertTrue(all(call.args == (123, signal.SIGKILL) for call in killpg.call_args_list))
        process.poll.assert_called_once_with()
        process.wait.assert_called_once_with()
        has_live_members.assert_called_once_with(123)

    def test_darwin_retries_group_signal_after_reaping(self):
        for retry_error in (None, ProcessLookupError("group no longer exists")):
            with self.subTest(retry_error=retry_error):
                events = []
                process = Mock(pid=123, poll=Mock(return_value=-signal.SIGKILL))
                process.wait.side_effect = lambda: events.append("wait")
                attempts = 0

                def killpg(pgid, sig):
                    nonlocal attempts
                    self.assertEqual((pgid, sig), (123, signal.SIGKILL))
                    attempts += 1
                    if attempts == 1:
                        events.append("initial")
                        raise PermissionError("permission denied")
                    events.append("retry")
                    if retry_error is not None:
                        raise retry_error

                with patch("engine.funsearch._process.sys.platform", "darwin"), \
                        patch("engine.funsearch._process.os.killpg", side_effect=killpg):
                    kill_group(process)

                self.assertEqual(events, ["initial", "wait", "retry"])
                self.assertEqual(attempts, 2)
                process.poll.assert_called_once_with()
                process.wait.assert_called_once_with()

    def test_darwin_permission_error_for_live_group_is_reported(self):
        process = Mock(pid=123, poll=Mock(return_value=-signal.SIGKILL))
        with patch("engine.funsearch._process.sys.platform", "darwin"), \
                patch("engine.funsearch._process.os.killpg",
                      side_effect=PermissionError("permission denied")) as killpg, \
                patch("engine.funsearch._process._darwin_group_has_live_members",
                      return_value=True) as has_live_members:
            with self.assertRaisesRegex(
                    PermissionError,
                    r"could not signal process group 123; descendants may remain"):
                kill_group(process)

        self.assertEqual(killpg.call_count, 2)
        process.poll.assert_called_once_with()
        process.wait.assert_called_once_with()
        has_live_members.assert_called_once_with(123)

    def test_darwin_group_inspection_distinguishes_zombies_from_live_members(self):
        from engine.funsearch._process import _darwin_group_has_live_members

        with patch("engine.funsearch._process.subprocess.run",
                   return_value=Mock(stdout=" 123 Z\n 123 Z+\n 456 S\n")):
            self.assertFalse(_darwin_group_has_live_members(123))
        with patch("engine.funsearch._process.subprocess.run",
                   return_value=Mock(stdout=" 123 Z\n 123 S+\n")):
            self.assertTrue(_darwin_group_has_live_members(123))

    def test_darwin_unknown_group_inspection_preserves_permission_error(self):
        unknown_queries = (
            ("ps unavailable", OSError("ps unavailable")),
            ("ps timeout", subprocess.TimeoutExpired(["ps"], 1)),
            ("malformed output", Mock(stdout="not a ps row\n")),
        )
        for label, query_result in unknown_queries:
            with self.subTest(query_result=label):
                process = Mock(pid=123, poll=Mock(return_value=-signal.SIGKILL))
                permission_error = PermissionError("permission denied")
                is_error = isinstance(query_result, BaseException)
                with patch("engine.funsearch._process.sys.platform", "darwin"), \
                        patch("engine.funsearch._process.os.killpg",
                              side_effect=permission_error) as killpg, \
                        patch("engine.funsearch._process.subprocess.run",
                              side_effect=query_result if is_error else None,
                              return_value=None if is_error else query_result) as ps:
                    with self.assertRaisesRegex(
                            PermissionError,
                            r"could not signal process group 123; descendants may remain") as caught:
                        kill_group(process)

                self.assertIs(caught.exception.__cause__, permission_error)
                self.assertEqual(killpg.call_count, 2)
                ps.assert_called_once()
                process.wait.assert_called_once_with()


class WorkerWatchdogTests(unittest.TestCase):
    def test_esrch_after_worker_exit_stops_watchdog_without_query_error(self):
        worker = Worker.__new__(Worker)

        class StopWhenInactive:
            def __init__(self):
                self.waits = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def wait(self, timeout):
                self.waits += 1
                if not worker._rss_watchdog_active:
                    worker._rss_watchdog_stop = True

            def notify_all(self):
                pass

        condition = StopWhenInactive()
        worker._rss_watchdog_condition = condition
        worker._rss_watchdog_thread = object()
        worker._rss_watchdog_active = True
        worker._rss_watchdog_stop = False
        worker._rss_watchdog_error = None
        worker._rss_limit_bytes = 1024
        # Model the exit race: proc_pidinfo sees ESRCH before Popen.poll has
        # collected the child's status.
        worker.process = Mock(pid=123, poll=Mock(return_value=None))

        with patch("engine.funsearch.workers.process_resident_bytes",
                   side_effect=OSError(errno.ESRCH, "No such process")) as resident_bytes, \
                patch("engine.funsearch.workers.kill_group") as kill_group:
            worker._watch_worker_rss()

        self.assertEqual(condition.waits, 2)
        self.assertFalse(worker._rss_watchdog_active)
        self.assertIsNone(worker._rss_watchdog_error)
        worker.process.poll.assert_called_once_with()
        resident_bytes.assert_called_once_with(123)
        kill_group.assert_not_called()

    def test_query_failure_during_activation_is_preserved(self):
        worker = Worker.__new__(Worker)
        worker._lock = threading.Lock()
        worker._closed = False
        worker._failure = None
        worker._rss_watchdog_error = None
        worker._recycle_due = False
        failure = "worker resident-set query failed: simulated failure"

        def activate_watchdog(active):
            if active:
                worker._rss_watchdog_error = failure

        with patch.object(worker, "_set_rss_watchdog_active", side_effect=activate_watchdog), \
                patch.object(worker, "_stale_output", return_value=False), \
                patch.object(worker, "_send"), \
                patch.object(worker, "_line", side_effect=EOFError), \
                patch.object(worker, "_dispose"), \
                patch.object(worker, "_with_stderr", side_effect=lambda message: message), \
                patch.object(worker, "_crash_message", return_value="worker crashed"), \
                patch.object(worker, "_start"):
            result = worker.score("candidate.so", 1)

        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["msg"], failure)

    def test_query_failure_overrides_score_timeout(self):
        worker = Worker.__new__(Worker)
        worker._lock = threading.Lock()
        worker._closed = False
        worker._failure = None
        worker._rss_watchdog_error = None
        worker._recycle_due = False
        failure = "worker resident-set query failed: simulated failure"

        def activate_watchdog(active):
            if active:
                worker._rss_watchdog_error = failure

        with patch.object(worker, "_set_rss_watchdog_active", side_effect=activate_watchdog), \
                patch.object(worker, "_stale_output", return_value=False), \
                patch.object(worker, "_send"), \
                patch.object(worker, "_line", side_effect=TimeoutError), \
                patch.object(worker, "_dispose"), \
                patch.object(worker, "_with_stderr", side_effect=lambda message: message), \
                patch.object(worker, "_start"):
            result = worker.score("candidate.so", 1)

        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["msg"], failure)

    def test_idle_watchdog_wake_does_not_inspect_worker(self):
        worker = Worker.__new__(Worker)

        class WakeTwiceThenStop:
            def __init__(self):
                self.waits = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def wait(self, timeout):
                self.waits += 1
                if self.waits == 2:
                    worker._rss_watchdog_stop = True

        condition = WakeTwiceThenStop()
        worker._rss_watchdog_condition = condition
        worker._rss_watchdog_active = False
        worker._rss_watchdog_stop = False
        worker.process = Mock(pid=123, poll=Mock(return_value=None))
        worker._rss_limit_bytes = 1024

        with patch("engine.funsearch.workers.process_resident_bytes", return_value=0) as resident_bytes:
            worker._watch_worker_rss()

        self.assertEqual(condition.waits, 2)
        worker.process.poll.assert_not_called()
        resident_bytes.assert_not_called()


PORTABLE_MAIN_DATA_SCAN = r"""#ifndef _GNU_SOURCE
#define _GNU_SOURCE 1
#endif
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#if defined(__APPLE__)
#include <mach-o/dyld.h>
#include <mach-o/loader.h>
#include <mach/mach.h>
typedef int (*main_data_scan_fn)(const unsigned char *, size_t, void *);
static int scan_main_data(main_data_scan_fn scan, void *context) {
    const struct mach_header *raw = _dyld_get_image_header(0);
    if (!raw || raw->magic != MH_MAGIC_64) return 0;
    const struct mach_header_64 *header = (const struct mach_header_64 *)raw;
    intptr_t slide = _dyld_get_image_vmaddr_slide(0);
    const struct load_command *command = (const struct load_command *)(header + 1);
    for (uint32_t i = 0; i < header->ncmds; ++i) {
        if (command->cmdsize < sizeof(*command)) return 0;
        if (command->cmd == LC_SEGMENT_64 &&
            command->cmdsize >= sizeof(struct segment_command_64)) {
            const struct segment_command_64 *segment =
                (const struct segment_command_64 *)command;
            if ((segment->initprot & (VM_PROT_READ | VM_PROT_WRITE)) ==
                (VM_PROT_READ | VM_PROT_WRITE)) {
                const unsigned char *base = (const unsigned char *)
                    ((uintptr_t)segment->vmaddr + (uintptr_t)slide);
                if (scan(base, (size_t)segment->vmsize, context)) return 1;
            }
        }
        command = (const struct load_command *)((const char *)command + command->cmdsize);
    }
    return 0;
}
#else
#include <link.h>
typedef int (*main_data_scan_fn)(const unsigned char *, size_t, void *);
struct main_data_scan_request { main_data_scan_fn scan; void *context; };
static int scan_main_image(struct dl_phdr_info *image, size_t info_size, void *opaque) {
    (void)info_size;
    if (image->dlpi_name[0]) return 0;
    struct main_data_scan_request *request = (struct main_data_scan_request *)opaque;
    for (int i = 0; i < image->dlpi_phnum; ++i) {
        const ElfW(Phdr) *segment = &image->dlpi_phdr[i];
        if (segment->p_type != PT_LOAD ||
            (segment->p_flags & (PF_R | PF_W)) != (PF_R | PF_W)) continue;
        const unsigned char *base = (const unsigned char *)
            ((uintptr_t)image->dlpi_addr + (uintptr_t)segment->p_vaddr);
        if (request->scan(base, (size_t)segment->p_memsz, request->context)) return 1;
    }
    return 0;
}
static int scan_main_data(main_data_scan_fn scan, void *context) {
    struct main_data_scan_request request = {scan, context};
    return dl_iterate_phdr(scan_main_image, &request);
}
#endif
static int nonce_region(const unsigned char *bytes, size_t size, void *opaque) {
    char *token = (char *)opaque;
    for (size_t i = 1; i + 33 <= size; ++i) {
        if (!bytes[i - 1] && !bytes[i + 32] &&
            strspn((const char *)bytes + i, "0123456789abcdef") == 32) {
            memcpy(token, bytes + i, 32);
            token[32] = '\0';
            return 1;
        }
    }
    return 0;
}
static int find_main_nonce(char token[33]) {
    token[0] = '\0';
    return scan_main_data(nonce_region, token);
}
struct byte_search { const unsigned char *needle; size_t length; };
static int bytes_region(const unsigned char *bytes, size_t size, void *opaque) {
    const struct byte_search *search = (const struct byte_search *)opaque;
    if (search->length > size) return 0;
    for (size_t i = 0; i + search->length <= size; ++i)
        if (!memcmp(bytes + i, search->needle, search->length)) return 1;
    return 0;
}
static int main_data_contains(const void *needle, size_t length) {
    struct byte_search search = {(const unsigned char *)needle, length};
    return scan_main_data(bytes_region, &search);
}
"""


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
        # A fast initialization failure may close stdin before the parent's
        # readiness write; that path reports the fatal startup exit code.
        with self.assertRaisesRegex(EvaluatorInitError, "fs_init returned 1|worker exited with code 3"):
            self.pool(instance="n=1,fail=1")

    def test_exit_three_is_init_error(self):
        binary = self.source("#!/bin/sh\nexit 3\n", "exit-three")
        binary.chmod(0o755)
        with patch("engine.funsearch.workers.worker_binary", return_value=binary):
            with self.assertRaisesRegex(EvaluatorInitError, "code 3"):
                self.pool()

    def test_candidate_exit_three_is_recoverable_after_startup(self):
        pool = self.pool()
        for body in ("double f(void) { exit(3); }",
                     "__attribute__((constructor)) static void die(void) { exit(3); }\n"
                     "double f(void) { return 1; }"):
            with self.subTest(body=body):
                source = self.source("#include <stdlib.h>\n" + body)
                ok, library, log = compile_candidate(self.cfg, source,
                                                     self.root / "exit-three-candidate", "final")
                self.assertTrue(ok, log)
                process = pool.workers[0].process
                result = pool.score(library, 5)
                self.assertEqual(result["status"], "ERROR")
                self.assertIn("worker crashed: exit code 3", result["msg"])
                self.assertIsNone(pool.workers[0]._failure)
                self.assertIsNot(pool.workers[0].process, process)
                self.assertEqual(pool.score(self.candidate("good"), 5)["status"], "OK")

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
        # Either startup diagnostic is valid; both must tear down the worker
        # that initialized successfully before the second worker failed.
        with self.assertRaisesRegex(EvaluatorInitError, "fs_init returned 1|worker exited with code 3"):
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
        source = self.source("#include <stdlib.h>\n"
                             "double f(void) { for (;;) { char *p = malloc(32 << 20);"
                             " if (!p) return -1; volatile char *pages = p;"
                             " for (size_t i = 0; i < (32u << 20); i += 4096) pages[i] = 1; } }")
        ok, library, log = compile_candidate(cfg, source, self.root / "hog", "try")
        self.assertTrue(ok, log)
        result = pool.score(library, 20)
        self.assertEqual(result["status"], "ERROR")
        if sys.platform == "darwin":
            # The native Mach watchdog and parent monitor enforce the same
            # cap; either can observe the overage first.
            self.assertIn("resident-set limit exceeded", result["msg"])
        else:
            # ASan enforces hard_rss_limit_mb on Linux.
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
        source = self.source(PORTABLE_MAIN_DATA_SCAN + r"""
#include <stdio.h>
static char token[33];
double f(void) {
    if (!find_main_nonce(token)) return -1;
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

    def test_request_token_is_wiped_before_candidate_constructors_run(self):
        # dlopen runs constructors, so the request buffer must no longer hold
        # "SCORE #<token>" by then. The needle is built at run time so the
        # candidate's own read-only literal cannot match.
        source = self.source(PORTABLE_MAIN_DATA_SCAN + r"""
static int found = -1;
__attribute__((constructor)) static void probe(void) {
    char needle[8] = "SCORE ";
    needle[6] = '#';
    found = main_data_contains(needle, 7);
}
double f(void) { return found == 0 ? 2 : found; }
""")
        ok, library, log = compile_candidate(self.cfg, source, self.root / "ctor-scan", "final")
        self.assertTrue(ok, log)
        self.assertEqual(self.pool().score(library, 5)["score"], 2)

    # Known limitation, documented in the README trust model: a candidate
    # thread can read stdin itself and answer the SYNC barrier while the
    # worker's main loop stays blocked in f(). Closing it needs the OS-level
    # isolation tracked as mc-v8f3.5; drop expectedFailure when that lands.
    @unittest.expectedFailure
    def test_candidate_reading_stdin_cannot_answer_the_barrier(self):
        source = self.source(PORTABLE_MAIN_DATA_SCAN + r"""
#include <pthread.h>
#include <stdio.h>
#include <unistd.h>
static char token[33];
static void *steal(void *unused) {
    char line[128];
    size_t n = 0;
    (void)unused;
    if (!find_main_nonce(token)) return NULL;
    for (int fd = 3; fd < 64; ++fd)
        dprintf(fd, "{\"nonce\":\"%s\",\"status\":\"OK\",\"score\":1e300,\"sig\":[],\"msg\":\"\"}\n", token);
    while (n + 1 < sizeof(line) && read(0, line + n, 1) == 1 && line[n] != '\n')
        ++n;
    line[n] = '\0';
    if (strncmp(line, "SYNC #", 6) == 0)
        for (int fd = 3; fd < 64; ++fd)
            dprintf(fd, "{\"sync\":\"%s\"}\n", line + 6);
    return NULL;
}
double f(void) {
    pthread_t thread;
    pthread_create(&thread, NULL, steal, NULL);
    for (;;) pause();
}
""")
        ok, library, log = compile_candidate(self.cfg, source, self.root / "steal-stdin", "final")
        self.assertTrue(ok, log)
        result = self.pool().score(library, 5)
        self.assertNotEqual(result["status"], "OK", result)

    @unittest.skipIf(sys.platform == "darwin",
                     "Darwin RSS fallback disables subprocess creation in workers")
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
        public = {"LC_ALL": "C", "TMPDIR": "/tmp", "DYLD_LIBRARY_PATH": "/opt/dyld",
                  "SOLVER_LICENSE_FILE": "/opt/l"}
        self.cfg.evaluator.env = ["SOLVER_*_FILE"]
        with patch.dict(os.environ, {**private, **public}):
            pool = self.pool(extra_env={"KEEP_ME": "1"})
        env = pool.workers[0].env
        self.assertFalse(set(private) & set(env))
        self.assertLessEqual(public.items(), env.items())
        self.assertEqual(env["KEEP_ME"], "1")
        self.assertIn("PATH", env)

    @unittest.skipUnless(sys.platform == "darwin", "external RSS monitoring is macOS-only")
    def test_rss_limit_survives_candidate_direct_exec(self):
        self.cfg.evaluator.memory_mb = 64
        python = json.dumps(sys.executable)
        script = json.dumps(
            "import mmap,time\n"
            "size=256*1024*1024\n"
            "memory=mmap.mmap(-1,size)\n"
            "for i in range(0,size,4096): memory[i]=1\n"
            "time.sleep(10)\n"
        )
        source = self.source(
            "#include <unistd.h>\n"
            "extern char **environ;\n"
            f"double f(void) {{ char *const args[] = {{{python}, \"-c\", {script}, NULL}};\n"
            f"    execve({python}, args, environ);\n"
            "    return 0;\n"
            "}\n"
        )
        ok, library, log = compile_candidate(self.cfg, source, self.root / "exec-rss", "final")
        self.assertTrue(ok, log)
        pool = self.pool()
        result = pool.score(library, 10)
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("worker resident-set limit exceeded", result["msg"])
        self.assertEqual(pool.score(self.candidate("good"), 5)["score"], 3.5)

    @unittest.skipUnless(sys.platform == "darwin", "external RSS monitoring is macOS-only")
    def test_rss_query_failure_aborts_live_worker(self):
        self.cfg.evaluator.memory_mb = 64
        should_fail = False
        failed_once = False

        def resident_bytes(pid):
            nonlocal failed_once
            if should_fail and pid != os.getpid() and not failed_once:
                failed_once = True
                raise OSError("simulated proc_pidinfo failure")
            return 0

        with patch("engine.funsearch.workers.process_resident_bytes", side_effect=resident_bytes):
            pool = self.pool()
            should_fail = True
            result = pool.score(self.candidate("loop"), 5)
            self.assertTrue(failed_once)
            self.assertEqual(result["status"], "ERROR")
            self.assertIn("worker resident-set query failed: simulated proc_pidinfo failure",
                          result["msg"])
            self.assertEqual(pool.score(self.candidate("good"), 5)["score"], 3.5)

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

    @unittest.skipIf(sys.platform == "darwin",
                     "Darwin RSS fallback disables subprocess creation in workers")
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
