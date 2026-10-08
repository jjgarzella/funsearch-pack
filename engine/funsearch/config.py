"""Problem configuration. No compilation or evaluator loading happens here."""

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
import math
import tomllib


class ConfigError(ValueError):
    """A problem configuration cannot be used."""


@dataclass
class Problem:
    name: str = ""
    instance: str = ""


@dataclass
class Candidate:
    language: str = "c"
    compile: str = "cc -O2 -shared -fPIC -o {out} {src}"
    compile_try: str = "cc -O1 -g -fsanitize=address,undefined -shared -fPIC -o {out} {src}"
    exports: list[str] = field(default_factory=list)


@dataclass
class Evaluator:
    build: str = "make -C evaluator"
    library: str = "evaluator/libevaluator.so"
    timeout_s: float = 30.0
    memory_mb: int = 2048
    # Extra variable names (fnmatch patterns) evaluator workers inherit.
    env: list[str] = field(default_factory=list)


@dataclass
class Search:
    islands: int = 4
    mutators: int = 3
    tasks_per_session: int = 5
    trial_budget: int = 3
    parents_per_task: int = 2
    workers: int = 2
    reset_period_s: float = 1800.0


@dataclass
class Stop:
    duration_s: float = 3600.0
    max_children: int = 100
    plateau_children: int = 0


@dataclass
class Mutator:
    model: str = "claude-haiku-4-5-20251001"


@dataclass
class Config:
    problem: Problem = field(default_factory=Problem)
    candidate: Candidate = field(default_factory=Candidate)
    evaluator: Evaluator = field(default_factory=Evaluator)
    search: Search = field(default_factory=Search)
    stop: Stop = field(default_factory=Stop)
    mutator: Mutator = field(default_factory=Mutator)

    def to_dict(self):
        return asdict(self)


def _check_type(key, value, default):
    if isinstance(default, list):
        valid = isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)
    elif isinstance(default, float):
        valid = type(value) in (int, float) and math.isfinite(value)
    else:
        valid = type(value) is type(default)
    if not valid:
        raise ConfigError(f"{key}: expected {type(default).__name__}, got {value!r}")


def apply_overrides(cfg: Config, overrides=()) -> Config:
    """Return a copy with typed dotted-key overrides (also accepts instance=S)."""
    result = Config(**{f.name: type(getattr(cfg, f.name))(**asdict(getattr(cfg, f.name))) for f in fields(cfg)})
    for override in overrides:
        key, sep, raw = override.partition("=")
        if not sep:
            raise ConfigError(f"override must be key=value: {override!r}")
        if key == "instance":
            key = "problem.instance"
        section, dot, name = key.partition(".")
        if not dot or section not in result.to_dict() or name not in result.to_dict()[section]:
            raise ConfigError(f"unknown configuration key: {key}")
        default = getattr(getattr(Config(), section), name)
        try:
            if isinstance(default, str):
                value = raw
            else:
                value = tomllib.loads("value=" + raw)["value"]
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            raise ConfigError(f"{key}: invalid override value {raw!r}") from exc
        _check_type(key, value, default)
        setattr(getattr(result, section), name, value)
    return result


def validate(cfg: Config, problem_dir) -> None:
    root = Path(problem_dir)
    for name in ("problem.md", "candidate.h", "seed.c"):
        if not (root / name).is_file():
            raise ConfigError(f"missing required file: {root / name}")
    defaults = Config().to_dict()
    for section, values in cfg.to_dict().items():
        for name, value in values.items():
            key = f"{section}.{name}"
            _check_type(key, value, defaults[section][name])
            if type(value) in (int, float):
                disabled_allowed = key == "stop.plateau_children"
                if value < 0 or (value == 0 and not disabled_allowed):
                    raise ConfigError(f"{key}: must be {'non-negative' if disabled_allowed else 'positive'}")
    if not cfg.candidate.exports:
        raise ConfigError("candidate.exports: must contain at least one symbol")
    for key, value in (("candidate.language", cfg.candidate.language),
                       ("candidate.compile", cfg.candidate.compile),
                       ("candidate.compile_try", cfg.candidate.compile_try),
                       ("evaluator.library", cfg.evaluator.library),
                       ("mutator.model", cfg.mutator.model)):
        if not value.strip():
            raise ConfigError(f"{key}: must not be empty")


def load_config(problem_dir, overrides=(), *, instance=None) -> Config:
    root = Path(problem_dir)
    try:
        with (root / "problem.toml").open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {root / 'problem.toml'}: {exc}") from exc
    cfg = Config()
    for section, values in data.items():
        if section not in cfg.to_dict():
            raise ConfigError(f"unknown configuration section: {section}")
        if not isinstance(values, dict):
            raise ConfigError(f"{section}: expected a table")
        target = getattr(cfg, section)
        for name, value in values.items():
            if name not in asdict(target):
                raise ConfigError(f"unknown configuration key: {section}.{name}")
            _check_type(f"{section}.{name}", value, getattr(target, name))
            setattr(target, name, value)
    cfg = apply_overrides(cfg, overrides)
    if instance is not None:
        cfg.problem.instance = instance
    validate(cfg, root)
    return cfg
