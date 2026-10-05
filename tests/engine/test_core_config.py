from pathlib import Path
import shutil
import tempfile
import unittest

from engine.funsearch.config import Config, ConfigError, apply_overrides, load_config

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "toy-problem"


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "problem"
        shutil.copytree(FIXTURE, self.root)

    def test_all_defaults(self):
        cfg = load_config(self.root)
        expected = Config().to_dict()
        expected["problem"] = {"name": "toy", "instance": "n=1"}
        expected["candidate"]["exports"] = ["f"]
        expected["evaluator"]["build"] = "mkdir -p evaluator && cc -shared -fPIC -I../../../include -o evaluator/libevaluator.so ../../worker/toy_eval.c"
        self.assertEqual(cfg.to_dict(), expected)
        self.assertEqual(cfg.search.islands, 4)
        self.assertEqual(cfg.stop.plateau_children, 0)
        self.assertEqual(cfg.mutator.model, "claude-haiku-4-5-20251001")

    def test_typed_overrides(self):
        cfg = load_config(self.root, ["search.islands=6", "stop.max_children=50", "stop.duration_s=0.5",
                                     "instance=q=5,d=3", "candidate.exports=[\"f\",\"g\"]"])
        self.assertEqual(cfg.search.islands, 6)
        self.assertEqual(cfg.stop.max_children, 50)
        self.assertEqual(cfg.stop.duration_s, 0.5)
        self.assertEqual(cfg.problem.instance, "q=5,d=3")
        self.assertEqual(cfg.candidate.exports, ["f", "g"])
        self.assertEqual(load_config(self.root, instance="opaque,=text").problem.instance, "opaque,=text")

    def test_copy_does_not_mutate_original(self):
        cfg = load_config(self.root)
        modified = apply_overrides(cfg, ["search.islands=7"])
        modified.candidate.exports.append("g")
        self.assertEqual(cfg.search.islands, 4)
        self.assertEqual(cfg.candidate.exports, ["f"])

    def test_bad_overrides(self):
        for override in ("wat=1", "search.unknown=1", "search.islands=true", "search.islands=1.5",
                         "search.islands=0", "stop.duration_s=nan", "stop.plateau_children=-1",
                         "candidate.exports=[]", "candidate.exports=[2]", "search.workers", "stop.max_children=oops"):
            with self.subTest(override=override), self.assertRaises(ConfigError):
                load_config(self.root, [override])

    def test_missing_required_files(self):
        for name in ("problem.toml", "problem.md", "candidate.h", "seed.c"):
            file = self.root / name
            saved = file.read_bytes()
            file.unlink()
            with self.subTest(name=name), self.assertRaisesRegex(ConfigError, name.replace(".", r"\.")):
                load_config(self.root)
            file.write_bytes(saved)

    def test_bad_toml(self):
        for text in ("[", "[unknown]\na=1", "search=3", "[search]\nworkers=false",
                     "[candidate]\nexports=[]", "[candidate]\nexports=[\"\"]", "[search]\nwrong=4"):
            (self.root / "problem.toml").write_text(text)
            with self.subTest(text=text), self.assertRaises(ConfigError):
                load_config(self.root)
