"""Candidate compilation and shared-library export checks (stdlib only)."""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

from ._process import LOG_BYTES, environment, run_command
from .normalize import _TOKEN


COMPILE_TIMEOUT_S = 60
_DIRECTIVE = re.compile(r"^[ \t]*(?:#|%:)[ \t]*(include_next|include|import|embed)\b[ \t]*(.*)$", re.M)
_HEADER = re.compile(r'"([^"\n]*)"|<([^>\n]*)>')


def source_policy_error(source):
    """Return why candidate source may not be compiled, or "".

    Compiler diagnostics are shown to the mutator, so a preprocessor read of
    an arbitrary file (an absolute or ../ include, #embed, .incbin) would leak
    that file into the model's context. Ordinary system and sibling headers
    such as <math.h> and "candidate.h" remain allowed.
    """
    if re.search(r"\bincbin\b", source):
        return "source policy: .incbin is not allowed"
    text = source.replace("\\\r\n", "").replace("\\\n", "")
    # Comments act as spaces; string literals stay intact.
    text = "".join(" " if match.lastgroup == "comment" else match.group()
                   for match in _TOKEN.finditer(text))
    for match in _DIRECTIVE.finditer(text):
        directive, operand = match.groups()
        if directive == "embed":
            return "source policy: #embed is not allowed"
        header = _HEADER.match(operand)
        if not header:
            return f"source policy: #{directive} must name a literal header"
        name = header.group(1) if header.group(1) is not None else header.group(2)
        if name.startswith("/") or ".." in Path(name).parts:
            return f"source policy: #{directive} may not use an absolute or parent path"
    return ""


def try_worker_env():
    """Environment needed to load ASan candidates in a separate worker.

    None removes FS_MEMORY_MB: ASan's large virtual shadow mapping cannot
    coexist with an RLIMIT_AS limit. Normal workers retain the configured limit.
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
    return {"LD_PRELOAD": preload,
            "ASAN_OPTIONS": "detect_leaks=0:abort_on_error=1",
            "FS_MEMORY_MB": None}


def check_exports(so_path, exports, extra_env=None):
    """Return (ok, message); only fall back to dlsym when nm is unavailable.

    The ctypes fallback runs in isolation so constructors (or an ASan library)
    cannot crash the engine process.
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
        script = ("import ctypes,json,sys; lib=ctypes.CDLL(sys.argv[1]); "
                  "print(json.dumps([n for n in json.loads(sys.argv[2]) "
                  "if not hasattr(lib,n)]))")
        try:
            result = subprocess.run([sys.executable, "-c", script, path, json.dumps(exports)],
                                    capture_output=True, text=True, timeout=60,
                                    env=environment(extra_env))
            if result.returncode:
                return False, f"cannot load library for export check: {result.stderr[:LOG_BYTES]}"
            missing = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return False, f"cannot inspect exports: {exc}"
    if missing:
        return False, "missing required exports: " + ", ".join(missing)
    return True, ""


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
        extra_env = try_worker_env() if mode == "try" and not shutil.which("nm") else None
        ok, message = check_exports(library, cfg.candidate.exports, extra_env)
    else:
        message = ""
    if message:
        log = (message + "\n" + log)[:LOG_BYTES]
    if not ok:
        library.unlink(missing_ok=True)
    return ok, library, log
