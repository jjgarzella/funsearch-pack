import os
from pathlib import Path
import shlex
import shutil
import sys
import time
from unittest.mock import patch

from engine.funsearch.compile import check_exports, compile_candidate, source_policy_error, try_worker_env
from engine.funsearch.evaluator import (EvaluatorBuildError, build_evaluator, evaluator_digest,
                                       snapshot_evaluator)
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

    def test_source_policy_lints_common_file_read_accidents(self):
        secret = self.root / "secret.txt"
        secret.write_text("TOP-SECRET-CONTENT\n")
        (self.root / "sub").mkdir()
        absolute = "may not use an absolute or parent path"
        asm, paste, assembler = "inline assembly", "token pasting", ".incbin and .include"
        blocked = ((f'#include "{secret}"\n', absolute),
                   (f"#include <{secret}>\n", absolute),
                   ('#include "../secret.txt"\n', absolute),
                   (f'#  /* split\n */ include "{secret}"\n', absolute),
                   (f'/* a */ /* b\n */ # /**/ include /* c */ "{secret}"\n', absolute),
                   (f'#\\\ninclude "{secret}"\n', absolute),
                   (f'%:include "{secret}"\n', absolute),
                   (f'#include_next "{secret}"\n', absolute),
                   (f'#define P "{secret}"\n#include P\n', "must name a literal header"),
                   (f'#embed "{secret}"\n', "#embed is not allowed"),
                   # A C lexer ends a stray quote at the line end; the
                   # include is live although a multi-line literal hides it.
                   (f"#if 0\n'\n#endif\n//' /*\n#include \"{secret}\"\n// */\n", absolute),
                   (f'const char *r = R"x(" /* )x";\n#include "{secret}"\n// */\n', absolute),
                   (f"int n = 1'/*';\n#include \"{secret}\"\n// */\n", absolute),
                   (f'??=include "{secret}"\n', absolute),
                   (f'#inc\\  \nlude "{secret}"\n', absolute),
                   (f'__asm__(".incbin \\"{secret}\\"");\n', asm),
                   (f'__asm__(".include \\"{secret}\\"");\n', asm),
                   ('#define P(a, b) a##b\nP(__as, m__)(".text");\n', paste),
                   ('#define P(a, b) a%:%:b\nP(__as, m__)(".text");\n', paste),
                   # The assembler reads files through section names too,
                   # where the asm rule does not apply.
                   (f'__attribute__((section(".text\\n.incbin \\"{secret}\\""))) int y;\n', assembler),
                   (f'__attribute__((section(".text\\n.include \\"{secret}\\""))) int y;\n', assembler),
                   (f'__attribute__((section(".text\\n.inc\\\nbin \\"{secret}\\""))) int y;\n', assembler),
                   (f'__attribute__((section(".text\\n.inc" "bin \\"{secret}\\""))) int y;\n', assembler),
                   (f'__attribute__((section(".text\\n.inc" /* x */ "bin \\"{secret}\\""))) int y;\n',
                    assembler),
                   (f'__attribute__((section(".text\\n.inc" // x\n u8"bin \\"{secret}\\""))) int y;\n',
                    assembler))
        for text, reason in blocked:
            with self.subTest(text=text):
                source = self.root / "sub" / "candidate.c"
                source.write_text(text + "double f(void) { return 1; }\n")
                for mode in ("try", "final"):
                    ok, library, log = compile_candidate(self.cfg, source, self.root / "policy", mode)
                    self.assertFalse(ok)
                    self.assertTrue(log.startswith("source policy lint: "), log)
                    self.assertIn(reason, log)
                    self.assertNotIn("TOP-SECRET", log)
                    self.assertFalse(library.exists())
        allowed = self.source('#include <math.h>\n#include "candidate.h"\n'
                              '// #include "/etc/passwd" in a comment is inert\n'
                              'const char *s = "#include </etc/passwd>";\n'
                              'double f(void) { return sqrt(4.0); }\n')
        (allowed.parent / "candidate.h").write_text("double f(void);\n")
        ok, _, log = compile_candidate(self.cfg, allowed, self.root / "allowed", "final")
        self.assertTrue(ok, log)

    def test_source_policy_is_fast_on_stacked_comments(self):
        # Regexes with a repeated lazy comment body took time doubling with
        # each stacked comment; the scan must stay near-linear.
        n = 4000
        cases = ('#include "candidate.h"\n' + "/* note */\n" * n,
                 "".join(f"int v{i}; /* a */ /* b */\n" for i in range(n)),
                 "/* a */ " * n + "\n",
                 '"a" ' + "/* a */ " * n + "x\n",
                 "int f(void){return 0;}\n" + "/**/" * n + "x\n",
                 "".join(f"/* c{i} */ int v{i};\n" for i in range(n)),
                 "/*\n" * n + "*/" + " " * n + "x\n",
                 "/*\n" * n + "*/#include <math.h>" + " " * n + "\n",
                 '"/*' * n + "*/x\n",
                 '"//' * n + "\nx\n")
        for text in cases:
            with self.subTest(text=text[:40]):
                started = time.monotonic()
                self.assertEqual(source_policy_error(text), "")
                self.assertLess(time.monotonic() - started, 1.0)

    def test_export_check_fallback_runs_constructors_in_candidate_environment(self):
        source = self.source('#include <stdio.h>\n#include <stdlib.h>\n'
                             '__attribute__((constructor)) static void leak(void) {\n'
                             '    const char *v = getenv("ANTHROPIC_API_KEY");\n'
                             '    fprintf(stderr, "seen=%s\\n", v ? v : "none"); exit(1);\n}\n'
                             'double f(void) { return 1; }\n')
        real_which = shutil.which
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-leak"}), \
                patch("engine.funsearch.compile.shutil.which",
                      side_effect=lambda name: None if name == "nm" else real_which(name)):
            ok, _, log = compile_candidate(self.cfg, source, self.root / "ctor", "final")
        self.assertFalse(ok)
        self.assertIn("seen=none", log)
        self.assertNotIn("sk-leak", log)

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
            self.assertEqual(check_exports(library, ["f"], {}), (True, ""))
            ok, log = check_exports(library, ["f", "g"], {})
        self.assertFalse(ok)
        self.assertIn("missing required exports: g", log)

    def test_nm_failure_does_not_fall_back(self):
        with patch("engine.funsearch.compile.subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stderr = "nm error"
            self.assertEqual(check_exports(self.root / "not.so", ["f"], {}), (False, "nm failed: nm error"))
            self.assertEqual(run.call_count, 1)

    def test_try_compilation_uses_sanitizers(self):
        library = self.candidate("good", "try")
        import subprocess
        output = subprocess.run(["nm", "-D", str(library)], capture_output=True, text=True, check=True).stdout
        self.assertIn("__asan", output)
        env = try_worker_env(self.cfg)
        self.assertTrue(Path(env["LD_PRELOAD"].split()[0]).is_file())
        limit = self.cfg.evaluator.memory_mb
        self.assertEqual(env["ASAN_OPTIONS"],
                         f"detect_leaks=0:abort_on_error=1:hard_rss_limit_mb={limit}"
                         f":max_allocation_size_mb={limit}:allocator_may_return_null=1")
        self.assertIsNone(env["FS_MEMORY_MB"])

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

    def test_library_directory_must_not_contain_the_runs(self):
        built = self.evaluator()
        for library in (self.problem / "libevaluator.so", self.root / "libevaluator.so"):
            with self.subTest(library=library):
                shutil.copy(built, library)
                self.cfg.evaluator.build = ""
                self.cfg.evaluator.library = os.path.relpath(library, self.problem)
                with self.assertRaisesRegex(EvaluatorBuildError, "own subdirectory"):
                    self.evaluator()

    def test_links_must_stay_inside_the_library_directory(self):
        # No export check is reached: links are refused before the library is loaded.
        library = self.problem / "evaluator" / "libevaluator.so"
        library.parent.mkdir(exist_ok=True)
        library.write_bytes(b"not loaded")
        (self.problem / "shared.jl").write_text("score() = 1\n")
        (library.parent / "data").mkdir()
        self.cfg.evaluator.build = ""
        for n, (name, target) in enumerate((("absolute.jl", str(library.parent / "data")),
                                            ("escaping.jl", "../shared.jl"),
                                            ("data/escaping.jl", "../../shared.jl"))):
            link = library.parent / name
            os.symlink(target, link)
            with self.subTest(link=name):
                with self.assertRaisesRegex(EvaluatorBuildError, "leaves"):
                    self.evaluator()
                with self.assertRaisesRegex(EvaluatorBuildError, "leaves"):
                    snapshot_evaluator(library, self.root / f"snapshot-{n}")
            link.unlink()
        # A link within the directory is kept.
        os.symlink("data", library.parent / "current")
        snapshot = snapshot_evaluator(library, self.root / "snapshot-inside")
        self.assertEqual(os.readlink(snapshot.parent / "current"), "data")

    def test_snapshot_copies_siblings_and_links_and_digests_them(self):
        library = self.evaluator()
        (library.parent / "scorer.jl").write_text("score() = 1\n")
        (library.parent / "data").mkdir()
        (library.parent / "data" / "table.txt").write_text("1 2 3\n")
        os.symlink("scorer.jl", library.parent / "current.jl")
        first = snapshot_evaluator(library, self.root / "first")
        self.assertEqual(first, self.root / "first" / "evaluator" / library.name)
        self.assertEqual(first.read_bytes(), library.read_bytes())
        self.assertEqual((first.parent / "data" / "table.txt").read_text(), "1 2 3\n")
        self.assertEqual(os.readlink(first.parent / "current.jl"), "scorer.jl")
        digest = evaluator_digest(first.parent)
        self.assertEqual(evaluator_digest(snapshot_evaluator(library, self.root / "same").parent), digest)
        # Any scoring resource, not just the library, distinguishes snapshots.
        for change in (lambda d: (d / "scorer.jl").write_text("score() = 2\n"),
                       lambda d: (d / "data" / "table.txt").write_text("1 2\n"),
                       lambda d: ((d / "current.jl").unlink(), os.symlink("data", d / "current.jl")),
                       lambda d: (d / "extra").write_text("")):
            copy = snapshot_evaluator(library, self.root / f"changed-{time.monotonic_ns()}").parent
            change(copy)
            with self.subTest(change=change):
                self.assertNotEqual(evaluator_digest(copy), digest)
