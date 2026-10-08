"""Create the mutator's self-contained task directory and prompt."""

from pathlib import Path
import random
import re
import shutil
import uuid

from ._protocol import CHILD_FILENAME, IDEA_PREFIX, TASK_FILENAME
from .config import Config
from .db import Database, NEXT_ISLAND
from .evolve import sample_parents


def _code_block(source: str, language="c") -> str:
    # A candidate can contain backticks in comments or strings.
    longest = max((len(m.group()) for m in re.finditer(r"`+", source)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{source}" + ("" if source.endswith("\n") else "\n") + f"{fence}\n"


def render_task(cfg: Config, statement: str, header: str, parents, children, task_dir: Path, *,
                duplicates=()) -> str:
    sections = [statement]
    sections.append("\n\n## Required exports\n\n" + _code_block(header))
    sections.append("\n## Parent programs (worst → best)\n")
    for index, parent in enumerate(sorted(parents, key=lambda p: (p.score, p.id))):
        sections.append(f"\n### v{index}\n\nScore: {parent.score}\n\nMessage:\n{parent.msg}\n\n" + _code_block(parent.source))
    sections.append("\n## Already tried on this island\n\n")
    if not children and not duplicates:
        sections.append("No children tried yet.\n")
    attempts = [(child.created_at, child.id, child, False) for child in children]
    attempts.extend((duplicate.created_at, duplicate.id, duplicate, True)
                    for duplicate in duplicates)
    for _, _, child, is_duplicate in sorted(attempts, key=lambda attempt: (attempt[0], attempt[1]))[-10:]:
        idea = child.idea.replace("\n", " ") or f"(no {IDEA_PREFIX} line)"
        if is_duplicate:
            sections.append(f"- {idea} — status: {child.status}; score: {child.score}; "
                            f"rejected as a behaviour duplicate; signature: {child.sig}\n")
        else:
            sections.append(f"- {idea} — status: {child.status}; score: {child.score}\n")
    sections.append(
        "\n## Instructions\n\n"
        f"Write a whole C file to `{CHILD_FILENAME}` in this task directory: `{task_dir}`.\n"
        f"Start the file with a one-line `{IDEA_PREFIX} <what you changed and why>`.\n"
        "Propose a structurally different mechanism from those listed above; reweighting an existing mechanism does not count. "
        "State what is structurally different in your IDEA line.\n"
        f"You may try up to {cfg.search.trial_budget} times, then submit.\n")
    return "".join(sections)


def create_task(db: Database, cfg: Config, problem_dir, run_dir, slot="", *,
                rng: random.Random | None = None) -> int:
    """Round-robin islands and write TASK.md; return the integer task id.

    File creation and DB updates are coordinated under a short write transaction.
    A failed write rolls back the task and island cursor and removes its directory.
    Directories left by killed clients are preserved under .orphan-* names when
    their uncommitted IDs are reused; committed task directories are untouched.
    """
    root = Path(problem_dir)
    statement = (root / "problem.md").read_bytes().decode("utf-8")
    header = (root / "candidate.h").read_bytes().decode("utf-8")
    rng = rng if rng is not None else random.Random()
    task_dir = None
    created_directory = False
    try:
        with db.transaction():
            island = db.get_state(NEXT_ISLAND, 0) % cfg.search.islands
            parents = sample_parents(db, island, cfg.search.parents_per_task, rng)
            if not parents:
                raise ValueError(f"island {island} has no OK parents; seed the database first")
            task = db.add_task(island, [parent.id for parent in parents], slot=slot)
            db.set_state(NEXT_ISLAND, (island + 1) % cfg.search.islands)
            task_dir = Path(run_dir).resolve() / "tasks" / str(task.id)
            try:
                task_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                # INSERT allocated an ID above every committed task while we
                # hold the writer lock. A preexisting directory at this ID can
                # only belong to an allocation whose transaction rolled back.
                # Preserve its contents for inspection, then retry publication.
                task_dir.rename(task_dir.with_name(f".orphan-{task.id}-{uuid.uuid4().hex}"))
                task_dir.mkdir(exist_ok=False)
            created_directory = True
            text = render_task(cfg, statement, header, parents, db.recent_children(island), task_dir,
                               duplicates=db.recent_behavior_duplicates(island))
            (task_dir / TASK_FILENAME).write_text(text, encoding="utf-8")
        return task.id
    except BaseException:
        if created_directory:
            shutil.rmtree(task_dir)
        raise
