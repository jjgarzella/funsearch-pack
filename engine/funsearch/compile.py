"""Candidate compilation and shared-library export checks (stdlib only)."""

from bisect import bisect_left
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import subprocess
import sys
import time

from ._process import LOG_BYTES, _StderrPrefix, kill_group, run_command, worker_environment


COMPILE_TIMEOUT_S = 60
EXPORT_TIMEOUT_S = 60
_DIRECTIVE_NAME = re.compile(r"(include_next|include|import|embed)\b")
_HEADER = re.compile(r'"([^"\n]*)"|<([^>\n]*)>')
_TRIGRAPH = re.compile(r"\?\?([=/'()!<>-])")
_TRIGRAPHS = dict(zip("=/'()!<>-", "#\\^[]|{}~"))
_SPLICES = (re.compile(r"\\\n"), re.compile(r"\\[ \t\f\v]*\n"))
_ASM = re.compile(r"(?<![A-Za-z0-9_$])(?:__asm__|__asm|asm)(?![A-Za-z0-9_$])")
_LITERAL_PREFIX = re.compile(r'(?:u8|u|U|L)?"')
_ASSEMBLER_READ = re.compile(r"incbin|\.include", re.I)


def _readings(source):
    """Yield each plausible translation-phase 1-2 text of source.

    Compilers differ on trigraphs (ISO modes replace them) and on spaces
    between a backslash and a newline (GCC and Clang still splice). A directive
    that one reading hides is live in another, so the policy checks them all.
    """
    text = source.replace("\r\n", "\n").replace("\r", "\n")
    for trigraphs in (False, True):
        base = _TRIGRAPH.sub(lambda match: _TRIGRAPHS[match[1]], text) if trigraphs else text
        for splice in _SPLICES:
            yield splice.sub("", base)


class _Gaps:
    """Where a run of blanks and comments starting at a position ends.

    A block comment ends at the first "*/" after its "/*", as in C. Comment
    ends are found by bisection and each comment's continuation is memoized,
    so scanning from every possible start stays near-linear in the source
    (regexes with a repeated comment body backtrack exponentially when
    comments are stacked). An unterminated comment ends the gap; the
    compiler rejects it anyway.
    """

    def __init__(self, text, newlines):
        self.text = text
        # newlines: literal concatenation (whitespace incl. newlines and //
        # comments); otherwise a directive line (spaces and block comments).
        self.blanks = " \t\f\v\n" if newlines else " \t\f\v"
        self.newlines = newlines
        self.closes = [match.start() for match in re.finditer(r"\*/", text)]
        self.line_ends = [match.start() for match in re.finditer(r"\n", text)] if newlines else []
        self.memo = {}

    def end(self, pos):
        text, visited = self.text, []
        while pos not in self.memo:
            visited.append(pos)
            while pos < len(text) and text[pos] in self.blanks:
                pos += 1
            if text.startswith("/*", pos):
                index = bisect_left(self.closes, pos + 2)
                if index == len(self.closes):
                    break
                pos = self.closes[index] + 2
            elif self.newlines and text.startswith("//", pos):
                index = bisect_left(self.line_ends, pos)
                if index == len(self.line_ends):
                    break
                pos = self.line_ends[index] + 1
            else:
                break
        pos = self.memo.get(pos, pos)
        for start in visited:
            self.memo[start] = pos
        return pos


def _directives(text):
    """Yield (directive, operand) for every possible directive start.

    Every physical line start, and the end of every block comment, may begin
    a directive. Deciding which ones lie inside a comment or a (raw) string
    literal needs a lexer that agrees with the compiler's, so the lint
    checks them all. It can reject inert text and may miss compiler-specific
    forms; it is not a complete C preprocessor. Comments may sit before and after "#" and before the operand.
    """
    gaps, seen = _Gaps(text, newlines=False), set()
    starts = [0, *(match.end() for match in re.finditer(r"\n|\*/", text))]
    for start in starts:
        pos = gaps.end(start)
        # Starts inside one comment all reach the same "#"; parse it once.
        if pos in seen:
            continue
        seen.add(pos)
        if text.startswith("#", pos):
            pos += 1
        elif text.startswith("%:", pos):
            pos += 2
        else:
            continue
        name = _DIRECTIVE_NAME.match(text, gaps.end(pos))
        if name:
            operand = gaps.end(name.end())
            line_end = text.find("\n", operand)
            yield name[1], text[operand:len(text) if line_end < 0 else line_end]


def _join_adjacent_literals(text):
    """Remove each closing quote, blanks/comments and opening quote between
    adjacent string literals, so split assembler keywords become visible."""
    gaps = _Gaps(text, newlines=True)
    pieces, cursor, quote = [], 0, text.find('"')
    while quote >= 0:
        joined = _LITERAL_PREFIX.match(text, gaps.end(quote + 1))
        if joined:
            pieces.append(text[cursor:quote])
            cursor = joined.end()
        quote = text.find('"', joined.end() if joined else quote + 1)
    pieces.append(text[cursor:])
    return "".join(pieces)


def source_policy_error(source):
    """Return why candidate source may not be compiled, or "".

    Best-effort lint for trusted local experiments: ordinary system and
    sibling headers such as <math.h> and "candidate.h" remain allowed, while
    suspicious includes and assembler text catch common mutator accidents.
    This may reject inert text or miss compiler-specific forms. It is not a
    security boundary and does not isolate compiler filesystem/network access
    or prevent arbitrary file disclosure. Native code runs as the invoking user.
    """
    for text in _readings(source):
        if _ASM.search(text):
            return "source policy lint: inline assembly (asm, __asm, __asm__) is not allowed"
        if "##" in text or "%:%:" in text:
            return "source policy lint: token pasting (##) is not allowed"
        if _ASSEMBLER_READ.search(text) or _ASSEMBLER_READ.search(_join_adjacent_literals(text)):
            return "source policy lint: .incbin and .include are not allowed"
        for directive, operand in _directives(text):
            if directive == "embed":
                return "source policy lint: #embed is not allowed"
            header = _HEADER.match(operand)
            if not header:
                return f"source policy lint: #{directive} must name a literal header"
            name = header.group(1) if header.group(1) is not None else header.group(2)
            if name.startswith("/") or ".." in Path(name).parts:
                return f"source policy lint: #{directive} may not use an absolute or parent path"
    return ""


def try_worker_env(cfg):
    """Environment needed to load ASan candidates in a separate worker.

    None removes FS_MEMORY_MB: ASan's large virtual shadow mapping cannot
    coexist with an RLIMIT_AS limit. ASan's own limits keep evaluator.memory_mb
    instead: the worker aborts once its RSS exceeds it, and a larger single
    allocation returns NULL. Normal workers retain the RLIMIT_AS limit.
    """
    compiler = shlex.split(os.environ.get("CC", "cc"))
    try:
        result = subprocess.run([*compiler, "-print-file-name=libasan.so"],
                                capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot locate ASan runtime: {exc}") from exc
    runtime = Path(result.stdout.strip())
    if not runtime.is_file():
        raise RuntimeError(f"cannot locate ASan runtime: {result.stdout.strip()!r}")
    preload = str(runtime.resolve())
    if os.environ.get("LD_PRELOAD"):
        preload += " " + os.environ["LD_PRELOAD"]
    memory_mb = int(cfg.evaluator.memory_mb)
    return {"LD_PRELOAD": preload,
            "ASAN_OPTIONS": (f"detect_leaks=0:abort_on_error=1:hard_rss_limit_mb={memory_mb}"
                             f":max_allocation_size_mb={memory_mb}:allocator_may_return_null=1"),
            "FS_MEMORY_MB": None}


def check_exports(so_path, exports, env):
    """Return (ok, message); only fall back to dlsym when nm is unavailable.

    The ctypes fallback runs in a separate process so a constructor crash
    cannot take down the engine. This does not sandbox native code. Those constructors are evaluator or
    candidate code, so the child gets env (from worker_environment()), not the
    engine's environment.
    """
    path = str(Path(so_path).resolve())
    if shutil.which("nm"):
        try:
            result = subprocess.run(["nm", "-D", "--defined-only", path],
                                    capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"cannot inspect exports: {exc}"
        if result.returncode:
            return False, f"nm failed: {result.stderr[:LOG_BYTES]}"
        defined = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                symbol = parts[-1]
                defined.add(symbol)
                # @@ marks the default version resolvable by dlsym.
                if "@@" in symbol:
                    defined.add(symbol.split("@@", 1)[0])
        missing = [name for name in exports if name not in defined]
    else:
        try:
            missing = _ctypes_exports(path, exports, env)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return False, f"cannot inspect exports: {exc}"
    if missing:
        return False, "missing required exports: " + ", ".join(missing)
    return True, ""


def _ctypes_exports(path, exports, env):
    """Load in a child with bounded diagnostics and a bounded JSON channel."""
    # Reserve the protocol channel before loading native code. Constructors'
    # stdout joins stderr, so even large output cannot corrupt the JSON result.
    script = ("import ctypes,json,os,sys; output=os.dup(1); os.dup2(2,1); "
              "lib=ctypes.CDLL(sys.argv[1]); "
              "missing=[n for n in json.loads(sys.argv[2]) if not hasattr(lib,n)]; "
              "stream=os.fdopen(output,'w'); stream.write(json.dumps(missing)); stream.close()")
    encoded_exports = json.dumps(exports)
    limit = len(encoded_exports.encode("utf-8"))
    process = subprocess.Popen([sys.executable, "-c", script, path, encoded_exports],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, env=env)
    stderr = _StderrPrefix(process.stderr)
    output = bytearray()
    deadline = time.monotonic() + EXPORT_TIMEOUT_S
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, EXPORT_TIMEOUT_S)
            readable, _, _ = select.select([process.stdout], [], [], remaining)
            if not readable:
                raise subprocess.TimeoutExpired(process.args, EXPORT_TIMEOUT_S)
            chunk = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > limit:
                raise ValueError("export check protocol exceeds expected size")
        process.wait(timeout=max(0, deadline - time.monotonic()))
    finally:
        kill_group(process)
        process.stdout.close()
        log = stderr.close()
    if process.returncode:
        raise ValueError(f"cannot load library for export check: {log or process.returncode}")
    missing = json.loads(output)
    if (not isinstance(missing, list) or
            any(not isinstance(name, str) or name not in exports for name in missing)):
        raise ValueError("invalid export check protocol")
    return missing


def compile_candidate(cfg, src_path, out_dir, mode):
    """Compile into out_dir/candidate.so; return (ok, absolute Path, log).

    Source and output paths are shell quoted. Each request must have its own
    out_dir when compilations run concurrently. Logs contain at most 4 KiB.
    """
    if mode not in {"try", "final"}:
        raise ValueError(f"unknown compile mode: {mode!r}")
    source = Path(src_path).resolve()
    directory = Path(out_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    library = directory / "candidate.so"
    # A failed command must never reuse an earlier successful artifact.
    library.unlink(missing_ok=True)
    policy = source_policy_error(source.read_text(errors="replace"))
    if policy:
        return False, library, policy
    template = cfg.candidate.compile_try if mode == "try" else cfg.candidate.compile
    substitutions = {"{src}": shlex.quote(str(source)), "{out}": shlex.quote(str(library))}
    command = re.sub(r"\{src\}|\{out\}", lambda match: substitutions[match[0]], template)
    ok, log = run_command(command, directory, COMPILE_TIMEOUT_S)
    if ok and not library.is_file():
        ok, message = False, f"compiler did not produce {library}"
    elif ok:
        extra_env = try_worker_env(cfg) if mode == "try" and not shutil.which("nm") else None
        ok, message = check_exports(library, cfg.candidate.exports, worker_environment(cfg, extra_env))
    else:
        message = ""
    if message:
        log = (message + "\n" + log)[:LOG_BYTES]
    if not ok:
        library.unlink(missing_ok=True)
    return ok, library, log
