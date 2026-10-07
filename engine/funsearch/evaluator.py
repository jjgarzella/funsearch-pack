"""Build and validate a problem's evaluator library."""

import hashlib
import json
import os
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
    # Every run copies the library's whole directory, which therefore must not
    # contain the runs themselves (as the problem directory or its parents do).
    if (root / "runs").is_relative_to(library.parent):
        raise EvaluatorBuildError(
            f"evaluator library must be in its own subdirectory (such as evaluator/), "
            f"not in {library.parent}: each run copies the library's directory")
    check_links(library.parent)
    ok, message = check_exports(library, ["fs_score"], worker_environment(cfg))
    if not ok:
        raise EvaluatorBuildError(f"invalid evaluator library: {message}")
    return library


def snapshot_evaluator(library, directory):
    """Copy the library's directory into directory/evaluator; return the copy.

    A run's workers (and later rescores) load this copy, so rebuilding or
    editing the evaluator in the problem directory never changes a live run's
    scoring function. The library's siblings come along because an evaluator
    may find resources next to itself, as cap-set finds capset.jl. Symlinks
    are copied as links, so check_links first confines them to the directory.
    """
    library = Path(library).resolve()
    check_links(library.parent)
    target = Path(directory) / "evaluator"
    shutil.copytree(library.parent, target, symlinks=True)
    return target / library.name


def check_links(directory):
    """Refuse symlinks that an evaluator snapshot could not own.

    A link copied verbatim must resolve inside the copy: an absolute link, or
    one that leaves the directory, would keep a run scoring with files outside
    its snapshot (and outside its digest), or resolve differently once the
    snapshot moves into the run.
    """
    root = Path(os.path.realpath(directory))
    for parent, directories, files in os.walk(root):
        for name in directories + files:
            path = Path(parent, name)
            if not path.is_symlink():
                continue
            target = os.readlink(path)
            if os.path.isabs(target) or not Path(os.path.realpath(path)).is_relative_to(root):
                raise EvaluatorBuildError(
                    f"evaluator symlink {path.relative_to(root)} -> {target} leaves {root}: "
                    f"each run copies links as links, so copy the resource into the "
                    f"directory or link within it")


def evaluator_digest(directory):
    """SHA-256 of a snapshot: every file's content and every symlink's target."""
    root = Path(directory)
    entries = []
    for parent, directories, files in os.walk(root):
        for name in directories + files:
            path = Path(parent, name)
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries.append(["link", relative, os.readlink(path)])
            elif path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as resource:
                    while chunk := resource.read(1024 * 1024):
                        digest.update(chunk)
                entries.append(["file", relative, digest.hexdigest()])
    return hashlib.sha256(json.dumps(sorted(entries)).encode()).hexdigest()
