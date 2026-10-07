"""Observe actual recovery subprocesses while one is paused during publication."""

from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from funsearch import cli, runtime
from funsearch.config import ConfigError
from funsearch.daemon import recover_outputs

root, role, entry = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
original_flock, original_replace = runtime.fcntl.flock, Path.replace
def observed_flock(handle, operation):
    if Path(handle.name).name == runtime.RECOVERY_LOCK:
        (root / (role + '-attempt')).touch()
    result = original_flock(handle, operation)
    if Path(handle.name).name == runtime.RECOVERY_LOCK:
        (root / (role + '-acquired')).touch()
    return result
def observed_replace(path, target):
    if path.name == 'best.c.tmp':
        (root / (role + '-publishing')).touch()
        if role == 'first':
            deadline = time.monotonic() + 10
            while not (root / 'release').exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError('test did not release paused recovery')
                time.sleep(0.01)
    result = original_replace(path, target)
    if path.name == 'summary.json.tmp':
        (root / (role + '-summary')).write_bytes(Path(target).read_bytes())
    return result
runtime.fcntl.flock, Path.replace = observed_flock, observed_replace
if entry == 'cli':
    sys.exit(cli.main(['run', 'recover', str(root)]))
root, metadata, cfg = runtime.read_run(root, require_db=False)
try:
    recover_outputs(root, metadata, cfg)
except ConfigError as exc:
    print(exc, file=sys.stderr)
    sys.exit(2)
"""


def recovery_process(test, root, role, entry="cli"):
    process = subprocess.Popen([sys.executable, "-c", BOOTSTRAP,
                                str(ROOT / "engine"), str(root), role, entry],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def cleanup():
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    test.addCleanup(cleanup)
    return process


def wait_for_path(test, path):
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    test.assertTrue(path.exists(), str(path))
