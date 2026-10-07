"""Literal-safe C token normalization for duplicate detection."""

import hashlib
import re

from ._protocol import IDEA_PREFIX

# Keep token boundaries: x + +y and x++ + y must remain different.
_TOKEN = re.compile(
    r'(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)|'
    r'(?P<literal>(?:u8|u|U|L)?(?:"(?:\\[\s\S]|[^"\\])*"|\'(?:\\[\s\S]|[^\'\\])*\'))|'
    r'(?P<space>\s+)|[A-Za-z_][A-Za-z_0-9]*|'
    r'(?:\.?[0-9])(?:[eEpP][+-]|[A-Za-z_0-9.])*|'
    r'>>=|<<=|\.\.\.|->|\+\+|--|<<|>>|<=|>=|==|!=|&&|\|\||'
    r'\*=|/=|%=|\+=|-=|&=|\^=|\|=|%:%:|%:|<:|:>|<%|%>|##|[^\s]', re.MULTILINE)

# Anchored to the shared IDEA_PREFIX constant so the writer (tasks.py's
# instructions) and this parser cannot silently drift.
_IDEA = re.compile(re.escape(IDEA_PREFIX) + r"\s*(.*)")


def normalize_source(source: str) -> str:
    """Canonical tokens unless preprocessing can observe whitespace or lines.

    Stringification observes gaps between argument tokens; source-position
    builtins observe physical lines. Headers can define either kind of macro,
    and token pasting can construct them. Preserve the original source in
    those cases rather than declaring different compiled behaviors duplicates.
    """
    original = source
    source = source.replace("\\\r\n", "").replace("\\\n", "")
    tokens = []
    directive = False
    directive_tokens = []
    previous_end = 0
    line_start = True
    whitespace_sensitive = False
    for match in _TOKEN.finditer(source):
        value = match.group()
        if match.lastgroup in ("comment", "space"):
            # A block comment is replaced by one space, even across lines.
            if "\n" in value and match.lastgroup == "space":
                if directive:
                    tokens.append("\n")
                directive = False
                directive_tokens = []
                line_start = True
            continue
        if line_start and value in ("#", "%:"):
            directive = True
            directive_tokens = []
            tokens.append("\n")
        if (value in ("__LINE__", "__builtin_LINE") or
                (directive and directive_tokens and value in
                 ("#", "%:", "##", "%:%:", "include", "include_next", "import", "embed"))):
            whitespace_sensitive = True
        if (directive and len(directive_tokens) == 3 and
                directive_tokens[1] == "define" and value == "(" and
                match.start() != previous_end):
            # #define F(x) and #define F (x) declare different kinds of macros.
            tokens.append("<macro-space>")
        line_start = False
        tokens.append(value)
        if directive:
            directive_tokens.append(value)
        previous_end = match.end()
    return "source\0" + original if whitespace_sensitive else " ".join(tokens).strip()


def normalized_hash(source: str) -> str:
    return hashlib.sha256(normalize_source(source).encode("utf-8")).hexdigest()


def extract_idea(source: str) -> str:
    for match in _TOKEN.finditer(source):
        if match.lastgroup == "comment":
            idea = _IDEA.match(match.group())
            if idea:
                return idea.group(1).strip()
    return ""
