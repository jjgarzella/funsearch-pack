"""Cross-language, process-level acceptance checks for the bundled example."""
import json
import os
from pathlib import Path
import re
import resource
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "build/funsearch-worker"
EVALUATOR = ROOT / "examples/cap-set/evaluator/libcapset.so"
BUILD = ROOT / "build/examples"
CHECKER = ROOT / "tools/check_cap.py"


class CapSetTests(unittest.TestCase):
    def run_worker(self, candidates=(), *, instance="n=6", env=None, cwd=None):
        requests = "".join(f"SCORE {BUILD / (name + '.so')}\n" for name in candidates) + "QUIT\n"
        return subprocess.run(
            [str(WORKER), str(EVALUATOR), instance], input=requests,
            text=True, capture_output=True, timeout=30, cwd=cwd,
            env={key: value for key, value in
                 {**os.environ, "FS_MEMORY_MB": "4096", **(env or {})}.items()
                 if value is not None},
        )

    def test_scores_failures_dumps_and_timings(self):
        with tempfile.TemporaryDirectory(prefix="capset dump ") as directory:
            candidates = ["seed", "better", "nan", "inf", "mutate", "missing", "seed"]
            process = self.run_worker(candidates, env={
                "FS_CAPSET_DUMP": directory, "FS_CAPSET_TIMINGS": "1",
            }, cwd=directory)  # Source loading must not depend on the worker cwd.
            self.assertEqual(process.returncode, 0, process.stderr)
            results = [json.loads(line) for line in process.stdout.splitlines()]
            self.assertEqual(len(results), len(candidates))
            seed, better, nan, inf, mutate, missing, repeated = results
            self.assertEqual(seed["status"], "OK")
            self.assertEqual(seed["score"], 64)
            self.assertEqual(seed["sig"], [16, 32, 64])
            self.assertEqual(better["status"], "OK")
            self.assertGreater(better["score"], seed["score"])
            self.assertEqual(better["sig"], [20, 40, 79])
            for invalid in (nan, inf):
                self.assertEqual(invalid["status"], "INVALID")
                self.assertIn("NaN/Inf", invalid["msg"])
            self.assertEqual(mutate["status"], "INVALID")
            self.assertIn("modified its input", mutate["msg"])
            self.assertEqual(missing["status"], "ERROR")
            self.assertIn("priority", missing["msg"])
            self.assertEqual(seed, repeated)  # A Julia exception must be recoverable.
            files = sorted(Path(directory).glob("*.txt"))
            self.assertEqual(len(files), 9)
            checked = subprocess.run(["python3", str(CHECKER), directory],
                                     text=True, capture_output=True, timeout=10)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            init_times = re.findall(r"capset fs_init ([0-9.]+) s", process.stderr)
            score_times = re.findall(r"capset fs_score ([0-9.]+) s", process.stderr)
            self.assertEqual(len(init_times), 1)
            self.assertEqual(len(score_times), 6)
            print(f"capset baseline={seed['score']}, better={better['score']}; "
                  f"fs_init={init_times[0]}s; fs_score(n=6)={score_times}s; "
                  f"{len(files)} cap dumps independently checked", flush=True)

    def test_crashing_candidate_has_no_score(self):
        process = self.run_worker(["segv"])
        self.assertLess(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout, "")

    def test_asan_trial_worker_can_initialize_julia(self):
        compiler = shlex.split(os.environ.get("CC", "cc"))
        runtime = subprocess.check_output([*compiler, "-print-file-name=libasan.so"],
                                          text=True, timeout=10).strip()
        self.assertTrue(Path(runtime).is_file())
        preload = runtime + (" " + os.environ["LD_PRELOAD"] if os.environ.get("LD_PRELOAD") else "")
        process = self.run_worker(["seed-asan"], env={
            "LD_PRELOAD": preload, "ASAN_OPTIONS": "detect_leaks=0:abort_on_error=1",
            "FS_MEMORY_MB": None, "LBT_USE_RTLD_DEEPBIND": None,
        })
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["score"], 64)

    def test_invalid_instances_fail_at_startup(self):
        for instance in ("", "n=0", "n=9", "n=no", "n=6junk", "n=6,n=5"):
            with self.subTest(instance=instance):
                process = self.run_worker(instance=instance)
                self.assertEqual(process.returncode, 3)
                self.assertIn("fatal", json.loads(process.stdout))
                self.assertIn("n=<integer>", process.stderr)

    def test_julia_source_failure_is_reported_at_startup(self):
        with tempfile.TemporaryDirectory(prefix="capset source \" ") as directory:
            library = Path(directory) / "libcapset.so"
            shutil.copy2(EVALUATOR, library)
            for source in (None, "this is not valid Julia !!!"):
                with self.subTest(source=source):
                    if source is not None:
                        (Path(directory) / "capset.jl").write_text(source)
                    process = subprocess.run(
                        [str(WORKER), str(library), "n=6"], input="QUIT\n",
                        text=True, capture_output=True, timeout=30,
                        env={**os.environ, "FS_MEMORY_MB": "4096"},
                    )
                    self.assertEqual(process.returncode, 3, process.stderr)
                    self.assertIn("fatal", json.loads(process.stdout))
                    self.assertIn("capset Julia exception", process.stderr)

    def test_small_instance_signature(self):
        process = self.run_worker(["seed"], instance="n=2")
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["score"], 4)
        self.assertEqual(result["sig"], [2, 4])

    def test_dump_failure_is_evaluator_error(self):
        with tempfile.TemporaryDirectory() as directory:
            obstruction = Path(directory) / "file"
            obstruction.write_text("not a directory")
            process = self.run_worker(["seed"], env={"FS_CAPSET_DUMP": str(obstruction)})
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result["status"], "ERROR")
            self.assertNotEqual(result["msg"], "")

    def test_python_checker_rejects_invalid_dumps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = {
                "line-n2.txt": "0 0\n1 0\n2 0\n",
                "duplicate-n2.txt": "0 0\n0 0\n",
                "dimension-n2.txt": "0\n",
                "range-n2.txt": "0 3\n",
                "noninteger-n2.txt": "0 x\n",
                "empty-n2.txt": "",
                "badname.txt": "0 0\n",
            }
            for name, content in cases.items():
                with self.subTest(name=name):
                    path = root / name
                    path.write_text(content)
                    process = subprocess.run(["python3", str(CHECKER), str(path)],
                                             text=True, capture_output=True, timeout=5)
                    self.assertEqual(process.returncode, 1)
                    self.assertIn("INVALID", process.stderr)
            empty = root / "empty-directory"
            empty.mkdir()
            process = subprocess.run(["python3", str(CHECKER), str(empty)],
                                     text=True, capture_output=True, timeout=5)
            self.assertEqual(process.returncode, 1)


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    unittest.main(verbosity=2)
