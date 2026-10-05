"""Island evolution; all random decisions use the caller's Random instance."""

import math
import random

from .config import Config
from .db import Database, Program


DEFAULT_TEMPERATURE = 0.1
DEFAULT_PERIOD = 30000
SIGNATURE_DIGITS = 8


def cluster_programs(programs: list[Program], *, digits=SIGNATURE_DIGITS) -> dict[tuple, list[Program]]:
    clusters = {}
    for program in programs:
        if program.status != "OK" or program.score is None:
            continue
        key = (program.score, tuple(round(value, digits) for value in program.sig))
        clusters.setdefault(key, []).append(program)
    return clusters


def _softmax_pick(values, weights, temperature, rng):
    # Range normalization keeps the schedule independent of problem score units.
    low, high = min(weights), max(weights)
    if high == low:
        probabilities = [1.0] * len(weights)
    else:
        scale = max(abs(high), abs(low), 1.0)
        # Scale before subtracting so opposite huge finite scores cannot overflow.
        span = high / scale - low / scale
        normalized = [(w / scale - high / scale) / span for w in weights]
        probabilities = [math.exp(w / temperature) for w in normalized]
    return rng.choices(values, weights=probabilities, k=1)[0]


def seed_islands(db: Database, cfg: Config, source: str, *, score: float,
                 sig=(), msg="") -> list[Program]:
    """Initialize each island exactly once from an authoritatively scored seed."""
    with db.transaction():
        if db.list_programs(active_only=False):
            raise ValueError("program database is already seeded")
        seeds = [db.add_program(i, source, score=score, sig=sig, msg=msg, program_id=0 if i == 0 else None)
                 for i in range(cfg.search.islands)]
        db.set_state("islands", cfg.search.islands)
        db.set_state("next_island", 0)
        return seeds


def sample_parents(db: Database, island: int, count: int, rng: random.Random,
                   *, temperature=DEFAULT_TEMPERATURE, period=DEFAULT_PERIOD) -> list[Program]:
    """Sample without replacement, clusters first and then shorter members."""
    if count < 1 or temperature <= 0 or period < 1:
        raise ValueError("count, temperature and period must be positive")
    programs = db.list_programs(island)
    clusters = cluster_programs(programs)
    current_temperature = temperature * (1 - (len(programs) % period) / period)
    parents = []
    for _ in range(min(count, sum(map(len, clusters.values())))):
        keys = list(clusters)
        key = _softmax_pick(keys, [key[0] for key in keys], current_temperature, rng)
        members = clusters[key]
        parent = _softmax_pick(members, [-len(p.source) for p in members], 1.0, rng)
        parents.append(parent)
        members.remove(parent)
        if not members:
            del clusters[key]
    return parents


def reset_weakest(db: Database, rng: random.Random, *, islands=None) -> list[int]:
    """Reset the weaker floor(N/2) islands from random survivors' best programs."""
    with db.transaction():
        count = islands if islands is not None else db.get_state("islands")
        if count is None:
            ids = sorted({p.island for p in db.list_programs()})
        else:
            ids = list(range(count))
        best = {island: db.best_program(island) for island in ids}
        if len(ids) < 2:
            return []
        # Shuffle before sorting to break score ties using injected randomness.
        rng.shuffle(ids)
        ids.sort(key=lambda i: best[i].score if best[i] is not None else -math.inf)
        weakest, survivors = ids[:len(ids) // 2], ids[len(ids) // 2:]
        survivors = [i for i in survivors if best[i] is not None]
        if not survivors:
            raise ValueError("cannot reset islands without an OK survivor")
        for island in weakest:
            seed = best[rng.choice(survivors)]
            db.archive_island(island)
            db.add_program(island, seed.source, score=seed.score, sig=seed.sig,
                           msg=seed.msg, idea=seed.idea, norm_hash=seed.norm_hash)
        db.increment_state("island_resets")
        return weakest
