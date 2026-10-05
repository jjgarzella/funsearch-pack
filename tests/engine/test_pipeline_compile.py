from pathlib import Path
import shlex
import sys
from unittest.mock import patch

from engine.funsearch.compile import check_exports, compile_candidate, try_worker_env
from engine.funsearch.evaluator import EvaluatorBuildError, build_evaluator
from tests.engine.pipeline_support import PipelineTestCase


class CompileTests(PipelineTestCase):
    def test_success_and_shell_quoted_paths(self):
        source = self.source("double f(void) { return 7; }", "source '$({out}).c")
        directory = self.root / "output ' $(touch injected)"
        ok, path, log = compile_candidate(self.cfg, source, directory, "final")
        self.assertTrue(ok, log)
        self.assertEqual(path, directory / "candidate.so")
        self.assertTrue(path.is_file())
        self.assertFalse((directory / "injected").exists())

    def test_compiler_error_and_log(self):
        source = self.source("this is not C;\n")
        ok, library, log = compile_candidate(self.cfg, source, self.root / "bad", "final")
        self.assertFalse(ok)
        self.assertFalse(library.exists())
        self.assertIn("error", log)
        self.assertLessEqual(len(log), 4096)

    def test_missing_export_and_undefined_export(self):
        for source in ("double other(void) { return 1; }",
                       "extern double f(void); double other(void) { return f(); }"):
            with self.subTest(source=source):
                ok, library, log = compile_candidate(
                    self.cfg, self.source(source), self.root / "missing", "final")
                self.assertFalse(ok)
                self.assertIn("missing required exports: f", log)
                self.assertFalse(library.exists())

    def test_all_exports_are_required(self):
        self.cfg.candidate.exports = ["f", "g"]
        ok, _, log = compile_candidate(self.cfg, self.source("double f(void) {return 1;}"),
                                       self.root / "multi", "final")
        self.assertFalse(ok)
        self.assertIn("missing required exports: g", log)

    def test_failure_never_reuses_stale_library(self):
        source = self.source("double f(void) { return 1; }")
        directory = self.root / "stale"
        self.assertTrue(compile_candidate(self.cfg, source, directory, "final")[0])
        self.cfg.candidate.compile = "true"
        ok, path, log = compile_candidate(self.cfg, source, directory, "final")
        self.assertFalse(ok)
        self.assertFalse(path.exists())
        self.assertIn("did not produce", log)

    def test_stderr_is_bounded(self):
        script = "import sys;sys.stderr.write('x'*20000);sys.exit(1)"
        self.cfg.candidate.compile = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
        ok, _, log = compile_candidate(self.cfg, self.source(""), self.root / "log", "final")
        self.assertFalse(ok)
        self.assertEqual(log, "x" * 4096)

    def test_timeout(self):
        self.cfg.candidate.compile = "sleep 10"
        with patch("engine.funsearch.compile.COMPILE_TIMEOUT_S", 0.1):
            ok, library, log = compile_candidate(self.cfg, self.source(""), self.root / "timeout", "final")
        self.assertFalse(ok)
        self.assertIn("timed out after 0.1s", log)
        self.assertFalse(library.exists())

    def test_ctypes_fallback_when_nm_is_missing(self):
        library = self.candidate("good")
        with patch("engine.funsearch.compile.shutil.which", return_value=None):
            self.assertEqual(check_exports(library, ["f"]), (True, ""))
            ok, log = check_exports(library, ["f", "g"])
        self.assertFalse(ok)
        self.assertIn("missing required exports: g", log)

    def test_nm_failure_does_not_fall_back(self):
        with patch("engine.funsearch.compile.subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stderr = "nm error"
            self.assertEqual(check_exports(self.root / "not.so", ["f"]), (False, "nm failed: nm error"))
            self.assertEqual(run.call_count, 1)

    def test_try_compilation_uses_sanitizers(self):
        library = self.candidate("good", "try")
        import subprocess
        output = subprocess.run(["nm", "-D", str(library)], capture_output=True, text=True, check=True).stdout
        self.assertIn("__asan", output)
        env = try_worker_env()
        self.assertTrue(Path(env["LD_PRELOAD"].split()[0]).is_file())
        self.assertEqual(env["ASAN_OPTIONS"], "detect_leaks=0:abort_on_error=1")

    def test_sanitized_ctypes_fallback(self):
        with patch("engine.funsearch.compile.shutil.which", return_value=None):
            self.candidate("good", "try")

    def test_invalid_mode(self):
        with self.assertRaises(ValueError):
            compile_candidate(self.cfg, self.problem / "seed.c", self.root, "unknown")


class EvaluatorTests(PipelineTestCase):
    def test_build_and_prebuilt_library(self):
        library = self.evaluator()
        self.assertEqual(library, self.problem / "evaluator" / "libevaluator.so")
        self.cfg.evaluator.build = ""
        self.assertEqual(self.evaluator(), library)

    def test_fixture_build_works_in_place(self):
        # Use a temporary hierarchy with the same relative layout as the repo.
        import shutil
        from tests.engine.pipeline_support import ROOT
        root = self.root / "fixture-repo"
        problem = root / "tests" / "fixtures" / "toy-problem"
        shutil.copytree(ROOT / "tests" / "fixtures" / "toy-problem", problem)
        shutil.copytree(ROOT / "tests" / "worker", root / "tests" / "worker")
        shutil.copytree(ROOT / "include", root / "include")
        from engine.funsearch.config import load_config
        self.assertTrue(build_evaluator(load_config(problem), problem).is_file())

    def test_build_failure(self):
        self.cfg.evaluator.build = "echo evaluator-failed >&2; exit 1"
        with self.assertRaisesRegex(EvaluatorBuildError, "evaluator-failed"):
            self.evaluator()

    def test_build_timeout(self):
        self.cfg.evaluator.build = "sleep 10"
        with patch("engine.funsearch.evaluator.BUILD_TIMEOUT_S", 0.1):
            with self.assertRaisesRegex(EvaluatorBuildError, "timed out"):
                self.evaluator()

    def test_missing_library(self):
        self.cfg.evaluator.build = ""
        with self.assertRaisesRegex(EvaluatorBuildError, "does not exist"):
            self.evaluator()

    def test_missing_fs_score(self):
        library = self.candidate("good")
        self.cfg.evaluator.build = ""
        self.cfg.evaluator.library = str(library)
        with self.assertRaisesRegex(EvaluatorBuildError, "missing required exports: fs_score"):
            self.evaluator()
