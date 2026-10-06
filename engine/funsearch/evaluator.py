"""Build and validate a problem's evaluator library."""

import hashlib
from pathlib import Path
import shutil

from ._process import run_command, worker_environment
from .compile import check_exports


BUILD_TIMEOUT_S = 15 * 60


class EvaluatorBuildError(RuntimeError):
    """An evaluator could not be built or does not satisfy the C contract."""


def build_evaluator(cfg, problem_dir):
    """Run the configured build, check fs_score, and return an absolute Path."""
    root = Path(problem_dir).resolve()
    if cfg.evaluator.build.strip():
        ok, log = run_command(cfg.evaluator.build, root, BUILD_TIMEOUT_S)
        if not ok:
            raise EvaluatorBuildError(f"evaluator build failed: {log}")
    library = (root / cfg.evaluator.library).resolve()
    if not library.is_file():
        raise EvaluatorBuildError(f"evaluator library does not exist: {library}")
    ok, message = check_exports(library, ["fs_score"], worker_environment(cfg))
    if not ok:
        raise EvaluatorBuildError(f"invalid evaluator library: {message}")
    return library


def library_digest(library):
    return hashlib.sha256(Path(library).read_bytes()).hexdigest()


def snapshot_evaluator(library, problem_dir, run_dir):
    """Copy the library's directory into run_dir/evaluator; return the copy.

    A run's workers (and later rescores) load this copy, so rebuilding or
    editing the evaluator in the problem directory never changes a live run's
    scoring function. The library's siblings come along because an evaluator
    may find resources next to itself, as cap-set finds capset.jl. Symlinks
    are copied as links; the problem's runs/ directory is never copied.
    """
    library = Path(library).resolve()
    runs = Path(problem_dir).resolve() / "runs"
    target = Path(run_dir) / "evaluator"
    shutil.copytree(library.parent, target, symlinks=True,
                    ignore=lambda directory, names: [n for n in names if Path(directory, n) == runs])
    return target / library.name
