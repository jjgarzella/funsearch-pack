"""Process-level checks for the worker's public protocol (stdlib only)."""
import json
import os
from pathlib import Path
import resource
import selectors
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build" / "test"
WORKER = ROOT / "build" / "funsearch-worker"


class WorkerTests(unittest.TestCase):
    def start(self, instance="", evaluator="toy_eval", env=None):
        process = subprocess.Popen(
            [str(WORKER), str(BUILD / f"lib{evaluator}.so"), instance],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env={**os.environ, **(env or {})},
        )
        self.addCleanup(self.cleanup, process)
        return process

    @staticmethod
    def cleanup(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def receive(self, process, request):
        process.stdin.write(request + "\n")
        process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(5), "worker did not reply")
        line = process.stdout.readline()
        self.assertTrue(line, "worker exited before replying")
        return json.loads(line)

    def score(self, process, candidate):
        return self.receive(process, f"SCORE {BUILD / ('lib' + candidate + '.so')}")

    def test_good_negative_and_missing_symbol(self):
        process = self.start("opaque=passed,unchanged=1")
        self.assertEqual(self.score(process, "good"), {
            "status": "OK", "score": 3.5, "sig": [3.5, 1], "msg": "ok",
        })
        negative = self.score(process, "neg")
        self.assertEqual(negative["status"], "INVALID")
        self.assertEqual(negative["msg"], "negative")
        missing = self.score(process, "nosym")
        self.assertEqual(missing["status"], "ERROR")
        self.assertEqual(missing["msg"], "missing f")
        self.assertEqual(self.score(process, "good")["score"], 3.5)

    def test_protocol_is_protected(self):
        process = self.start()
        self.assertEqual(self.score(process, "chatty")["score"], 1)
        stdout, stderr = process.communicate("QUIT\n", timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stdout, "")
        self.assertIn("chatty candidate on stdout", stderr)
        self.assertIn("toy evaluator init on stdout", stderr)
        self.assertIn("toy evaluator score on stdout", stderr)

    def test_crash_has_no_reply(self):
        process = self.start()
        stdout, _ = process.communicate(f"SCORE {BUILD / 'libsegv.so'}\n", timeout=5)
        self.assertLess(process.returncode, 0)
        self.assertEqual(stdout, "")

    def test_hang_has_no_reply_and_can_be_killed(self):
        process = self.start()
        process.stdin.write(f"SCORE {BUILD / 'libloop.so'}\n")
        process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertEqual(selector.select(2), [])
        self.assertIsNone(process.poll())
        process.kill()
        stdout, _ = process.communicate(timeout=5)
        self.assertLess(process.returncode, 0)
        self.assertEqual(stdout, "")

    def test_init_failure(self):
        process = self.start("n=6,fail=1")
        stdout, _ = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 3)
        self.assertEqual(json.loads(stdout), {"fatal": "fs_init returned 1"})

    def test_quit_and_eof_call_fini(self):
        with tempfile.TemporaryDirectory() as directory:
            for request in ("QUIT\n", ""):
                mark = Path(directory) / ("quit" if request else "eof")
                process = self.start(env={"TOY_FINI_MARK": str(mark)})
                stdout, _ = process.communicate(request, timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(stdout, "")
                self.assertTrue(mark.exists())

    def test_rebuild_same_path(self):
        process = self.start()
        with tempfile.TemporaryDirectory(prefix="worker space ") as directory:
            source = Path(directory) / "candidate.c"
            library = Path(directory) / "candidate.so"
            for value in (4, 9, 2):
                source.write_text(f"double f(void) {{ return {value}; }}\n")
                subprocess.run([
                    *shlex.split(os.environ.get("CC", "cc")),
                    "-shared", "-fPIC", "-o", str(library), str(source),
                ], check=True, capture_output=True)
                self.assertEqual(self.receive(process, f"SCORE {library}")["score"], value)

    def test_bad_request_and_load_failure_are_recoverable(self):
        process = self.start()
        for request in ("", "HELLO", "SCORE", "SCORE ", "QUIT extra"):
            self.assertEqual(self.receive(process, request), {
                "status": "ERROR", "score": 0, "sig": [], "msg": "bad request",
            })
        failure = self.receive(process, f"SCORE {BUILD / 'missing.so'}")
        self.assertEqual(failure["status"], "ERROR")
        self.assertIn("missing.so", failure["msg"])
        self.assertEqual(self.score(process, "good")["status"], "OK")

    def test_optional_hooks_and_json_escaping(self):
        process = self.start("escape", "edge_eval")
        result = self.score(process, "good")
        self.assertEqual(result["msg"],
                         'quote" slash\\ newline\n tab\t back\b form\f return\r low\x01')
        stdout, _ = process.communicate("QUIT\n", timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stdout, "")

    def test_result_bounds(self):
        process = self.start("unterminated", "edge_eval")
        result = self.score(process, "good")
        self.assertEqual(result["msg"], "x" * 255)
        self.assertEqual(result["sig"], list(range(8)))
        process = self.start("negative-nsig", "edge_eval")
        self.assertEqual(self.score(process, "good")["sig"], [])

    def test_non_finite_scores(self):
        for instance in ("nan", "infinity"):
            with self.subTest(instance=instance):
                process = self.start(instance, "edge_eval")
                result = self.score(process, "good")
                self.assertEqual(result["status"], "ERROR")
                self.assertIsNone(result["score"])
                self.assertEqual(result["msg"], "non-finite score")

    def test_rejection_keeps_its_verdict_with_non_finite_values(self):
        # score and sig are meaningful only for FS_OK; an INVALID result that
        # leaves them NaN must keep its status and diagnostic message.
        process = self.start("invalid-nan", "edge_eval")
        self.assertEqual(self.score(process, "good"), {
            "status": "INVALID", "score": None, "sig": [], "msg": "cap has a line",
        })

    def test_non_finite_signature_is_an_error(self):
        process = self.start("nan-sig", "edge_eval")
        self.assertEqual(self.score(process, "good"), {
            "status": "ERROR", "score": 2, "sig": [], "msg": "non-finite signature",
        })

    def test_nonce_is_echoed_and_removed_from_later_replies(self):
        process = self.start()
        good = BUILD / "libgood.so"
        tagged = self.receive(process, f"SCORE #0123abcd {good}")
        self.assertEqual(tagged.pop("nonce"), "0123abcd")
        self.assertEqual(tagged, self.score(process, "good"))
        missing = self.receive(process, f"SCORE #ff {BUILD / 'missing.so'}")
        self.assertEqual((missing["nonce"], missing["status"]), ("ff", "ERROR"))
        self.assertNotIn("nonce", self.score(process, "good"))
        for request in (f"SCORE # {good}", f"SCORE #XYZ {good}", f"SCORE #{'a' * 65} {good}",
                        "SCORE #abc", f"SCORE #abc{good}"):
            with self.subTest(request=request):
                self.assertEqual(self.receive(process, request), {
                    "status": "ERROR", "score": 0, "sig": [], "msg": "bad request",
                })

    def test_memory_limit(self):
        process = self.start("limit", "edge_eval", {"FS_MEMORY_MB": "128"})
        result = self.score(process, "good")
        self.assertEqual(result["score"], 128)
        self.assertEqual(result["sig"], [128])
        process = self.start(env={"FS_MEMORY_MB": "bad"})
        stdout, _ = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 3)
        self.assertEqual(json.loads(stdout), {"fatal": "invalid FS_MEMORY_MB"})

    def test_required_symbol_and_evaluator_load_failure(self):
        for evaluator in ("nosym", "nonexistent"):
            process = self.start(evaluator=evaluator)
            stdout, _ = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 3)
            self.assertIn("fatal", json.loads(stdout))
            if evaluator == "nosym":
                self.assertEqual(json.loads(stdout)["fatal"], "missing fs_score")


if __name__ == "__main__":
    # Crash checks should not create core dumps in the checkout.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    unittest.main(verbosity=2)
