"""Real C build fixtures shared by pipeline integration tests."""

from pathlib import Path
import shlex
import shutil
import tempfile
import unittest

from engine.funsearch.compile import compile_candidate
from engine.funsearch.config import load_config
from engine.funsearch.evaluator import build_evaluator


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "toy-problem"


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="funsearch pipeline ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.problem = self.root / "problem"
        # CLI check builds the fixture in place; tests must start from source.
        shutil.copytree(FIXTURE, self.problem, ignore=shutil.ignore_patterns("*.so", "*.o", "__pycache__"))
        self.cfg = load_config(self.problem)
        # The fixture's relative command is usable in situ. For a temporary
        # copy, point at exactly the same toy evaluator and public header.
        self.cfg.evaluator.build = (
            "mkdir -p evaluator && cc -shared -fPIC "
            f"-I{shlex.quote(str(ROOT / 'include'))} -o evaluator/libevaluator.so "
            f"{shlex.quote(str(ROOT / 'tests' / 'worker' / 'toy_eval.c'))}")

    def evaluator(self):
        return build_evaluator(self.cfg, self.problem)

    def candidate(self, name, mode="final"):
        saved = self.cfg.candidate.exports
        if name == "nosym":
            self.cfg.candidate.exports = ["other"]
        try:
            ok, library, log = compile_candidate(
                self.cfg, ROOT / "tests" / "worker" / f"{name}.c",
                self.root / f"{name}-{mode}", mode)
        finally:
            self.cfg.candidate.exports = saved
        self.assertTrue(ok, log)
        return library

    def source(self, text, name="custom.c"):
        path = self.root / name
        path.write_text(text)
        return path
