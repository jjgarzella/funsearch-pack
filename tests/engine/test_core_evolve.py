from collections import Counter
import math
from pathlib import Path
import random
import tempfile
import unittest

from engine.funsearch.config import Config
from engine.funsearch.db import Database
from engine.funsearch.evolve import cluster_programs, reset_weakest, sample_parents, seed_islands


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "db.sqlite")
        self.addCleanup(self.db.close)
        self.cfg = Config()

    def test_seed_every_island(self):
        seeds = seed_islands(self.db, self.cfg, "seed", score=0, sig=[1], msg="seed")
        self.assertEqual([p.island for p in seeds], [0, 1, 2, 3])
        self.assertTrue(all(p.source == "seed" and p.sig == [1] for p in seeds))
        with self.assertRaises(ValueError):
            seed_islands(self.db, self.cfg, "seed", score=0)

    def test_rounded_clusters_exclude_invalid(self):
        first = self.db.add_program(0, "a", score=1, sig=[0.123456789])
        second = self.db.add_program(0, "b", score=1, sig=[0.1234567891])
        self.db.add_program(0, "bad", status="INVALID", score=100)
        clusters = cluster_programs(self.db.list_programs())
        self.assertEqual(list(clusters.values()), [[first, second]])

    def test_sampling_deterministic_and_distinct(self):
        for score in range(8):
            self.db.add_program(0, f"source{score}", score=score)
        def draws():
            rng = random.Random(42)
            return [[p.id for p in sample_parents(self.db, 0, 3, rng)] for _ in range(20)]
        self.assertEqual(draws(), draws())
        self.assertTrue(all(len(set(ids)) == 3 for ids in draws()))
        self.assertEqual(len(sample_parents(self.db, 0, 20, random.Random(1))), 8)
        self.assertEqual(sample_parents(self.db, 1, 2, random.Random(1)), [])

    def test_prefers_higher_scores_and_shorter_members(self):
        low = self.db.add_program(0, "low", score=0)
        short = self.db.add_program(0, "short", score=10, sig=[1])
        long = self.db.add_program(0, "long" * 100, score=10, sig=[1])
        rng = random.Random(123)
        counts = Counter(sample_parents(self.db, 0, 1, rng)[0].id for _ in range(1000))
        self.assertGreater(counts[short.id] + counts[long.id], 950)
        self.assertGreater(counts[short.id], counts[long.id])
        self.assertLess(counts[low.id], 50)

    def test_extreme_finite_scores_and_invalid_schedule(self):
        self.db.add_program(0, "negative", score=-1e308)
        best = self.db.add_program(0, "positive", score=1e308)
        self.assertEqual(sample_parents(self.db, 0, 1, random.Random(0), period=2)[0], best)
        for kwargs in ({"temperature": 0}, {"period": 0}):
            with self.assertRaises(ValueError):
                sample_parents(self.db, 0, 1, random.Random(0), **kwargs)

    def test_temperature_cools_and_resets_at_period_boundary(self):
        class CapturingRandom(random.Random):
            def choices(self, population, *, weights, k):
                self.weights.append(list(weights))
                return [population[-1]]

        rng = CapturingRandom(0)
        self.db.add_program(0, "low", score=0)
        self.db.add_program(0, "high", score=1)
        # Counts 2, 3, 4, 5 cool toward the boundary, reset, then cool again.
        for count, temperature in ((2, 0.5), (3, 0.25), (4, 1.0), (5, 0.75)):
            with self.subTest(count=count):
                rng.weights = []
                sample_parents(self.db, 0, 1, rng, temperature=1.0, period=4)
                self.assertAlmostEqual(rng.weights[0][0], math.exp(-1 / temperature))
                self.assertEqual(rng.weights[0][1], 1)
            self.db.add_program(0, f"invalid-{count}", status="INVALID")

    def test_reset_preserves_history_and_reseeds_weak_half(self):
        seeds = seed_islands(self.db, self.cfg, "seed", score=0)
        for island in range(4):
            self.db.add_program(island, f"winner{island}", score=island + 1, parent_ids=[seeds[island].id])
        reset = reset_weakest(self.db, random.Random(12))
        self.assertEqual(reset, [0, 1])
        for island in reset:
            active = self.db.list_programs(island)
            self.assertEqual(len(active), 1)
            self.assertIn(active[0].source, ("winner2", "winner3"))
            self.assertEqual(active[0].parent_ids, [])
            self.assertEqual(len(self.db.list_programs(island, active_only=False)), 3)
        self.assertEqual(len(self.db.list_programs(2)), 2)
        self.assertEqual(self.db.get_state("island_resets"), 1)

    def test_ties_reset_deterministically_and_single_island_noop(self):
        seed_islands(self.db, self.cfg, "seed", score=0)
        first = reset_weakest(self.db, random.Random(7))
        with Database(Path(self.temp.name) / "other.sqlite") as other:
            seed_islands(other, self.cfg, "seed", score=0)
            self.assertEqual(reset_weakest(other, random.Random(7)), first)
        self.assertEqual(reset_weakest(self.db, random.Random(1), islands=1), [])
