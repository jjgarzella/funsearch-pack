"""Shared literals of the mutator task protocol.

The file a mutator session is told to write, and the convention it is told
to start that file with, were previously duplicated as bare string literals
across the module that renders TASK.md's instructions (tasks.py), the one
that parses what was written (normalize.py), and the tool guard that
enforces which files a mutator may touch (scripts/mutator_guard.py).
Centralizing them here means changing the contract is one edit, not four,
and a guard/instructions drift becomes an import-time name instead of a
silent mismatch. agents/mutator/prompt.template.md restates these as prose
for the model and cannot import them; keep it in sync by hand.
"""

TASK_FILENAME = "TASK.md"
CHILD_FILENAME = "child.c"
IDEA_PREFIX = "// IDEA:"
