"""Build and validate a problem's evaluator library."""

from pathlib import Path

from ._process import run_command
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
    ok, message = check_exports(library, ["fs_score"])
    if not ok:
        raise EvaluatorBuildError(f"invalid evaluator library: {message}")
    return library
