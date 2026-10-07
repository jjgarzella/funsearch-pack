"""Real hook deadlines, process-group cleanup, and daemon finalization."""

import contextlib
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from unittest.mock import patch

from engine.funsearch import cli
from engine.funsearch.db import Database
from engine.funsearch.runtime import engine_alive, hold_engine_lock, pid_alive
from tests.engine.pipeline_support import PipelineTestCase


class HookTimeoutTests(PipelineTestCase):
    def test_start_and_finish_timeouts_finalize_and_kill_descendants(self):
        script = self.source("""import os, subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[1])
(root / 'hook.group').write_text(str(os.getpgrp()))
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
(root / 'hook.child').write_text(str(child.pid))
time.sleep(60)
""", "hung-hook.py")
        finish = self.source("import sys\nfrom pathlib import Path\n"
                             "Path(sys.argv[1], 'finished.marker').touch()\n", "finish.py")
        bootstrap = """import os, sys
from engine.funsearch import daemon, runtime
runtime.HOOK_TIMEOUT_S = 0.5
daemon.serve(sys.argv[1], os.open(os.devnull, os.O_WRONLY),
             snapshot_period_s=60, stop_grace_s=1, abandon_period_s=60, busy_retry_s=1)
"""
        for phase in ("start", "finish"):
            with self.subTest(phase=phase):
                root = self.problem / "runs" / phase
                command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
                args = ["run", "start", str(self.problem), "--run-id", phase,
                        "--set", "search.workers=1", "--set", "stop.duration_s=0.1",
                        "--set", "evaluator.build=" + self.cfg.evaluator.build,
                        f"--on-{phase}", command]
                if phase == "start":
                    args += ["--on-finish", f"{shlex.quote(sys.executable)} {shlex.quote(str(finish))}"]
                # Prepare a real run; serve below owns its lifecycle in a
                # subprocess with a short test deadline and real worker pools.
                with patch("engine.funsearch.cli.daemonize"), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(args), 0)
                self.addCleanup(self.kill_hook_group, root)
                started = time.monotonic()
                process = subprocess.Popen([sys.executable, "-c", bootstrap, str(root)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           start_new_session=True)
                try:
                    output, errors = process.communicate(timeout=10)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                self.assertEqual(process.returncode, 0, output + errors)
                self.assertLess(time.monotonic() - started, 5)
                self.assertIn(b"hook timed out after 0.5s", errors)
                summary = json.loads((root / "summary.json").read_text())
                self.assertEqual(summary["status"], "failed")
                self.assertIn(f"on-{phase} hook: hook timed out", summary["reason"])
                with Database(root / "db.sqlite", readonly=True) as db:
                    self.assertEqual(db.get_state("status"), "failed")
                for filename in ("best.c", "summary.json"):
                    self.assertTrue((root / filename).exists(), filename)
                self.assertFalse((root / "engine.pid").exists())
                self.assertFalse(engine_alive(root))
                with hold_engine_lock(root, timeout_s=0.05):
                    pass
                child = int((root / "hook.child").read_text())
                deadline = time.monotonic() + 2
                while pid_alive(child) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(pid_alive(child), "hook descendant survived timeout")
                if phase == "start":
                    self.assertTrue((root / "finished.marker").exists())

    @staticmethod
    def kill_hook_group(root):
        marker = root / "hook.group"
        if marker.exists():
            try:
                os.killpg(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
