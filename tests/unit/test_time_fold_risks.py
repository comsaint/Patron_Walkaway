import pathlib
import sys
import unittest
from datetime import datetime, timezone


def _project_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]

def _trainer_dir() -> pathlib.Path:
    return _project_root() / "trainer"


def _import_time_fold_top_level():
    """Import `time_fold` by adding `trainer/` to sys.path.

    This mirrors how scripts were executed from within `trainer/`.
    It also verifies that the module works in the standalone-import mode
    (not just as `trainer.time_fold`), ensuring backward compatibility.
    """
    trainer_dir = str(_trainer_dir())
    sys.path.insert(0, trainer_dir)
    try:
        return __import__("time_fold")
    finally:
        # Remove the path we added, but keep the imported module in sys.modules.
        sys.path.remove(trainer_dir)


class TestTimeFoldRisks(unittest.TestCase):
    def test_import_trainer_time_fold_from_project_root_currently_fails(self):
        # R2 is now fixed: importing as `trainer.time_fold` from project root
        # must succeed.  The test name is kept for historical traceability.
        root = _project_root()
        sys.path.insert(0, str(root))
        try:
            mod = __import__("trainer.time_fold", fromlist=["get_monthly_chunks"])
            self.assertTrue(hasattr(mod, "get_monthly_chunks"))
            self.assertTrue(callable(mod.get_monthly_chunks))
        finally:
            sys.path.remove(str(root))

    def test_import_trainer_time_fold_from_project_root_should_work(self):
        # Guardrail: importing as a package from repo root must succeed.
        root = _project_root()
        sys.path.insert(0, str(root))
        try:
            mod = __import__("trainer.time_fold", fromlist=["get_monthly_chunks"])
            self.assertTrue(hasattr(mod, "get_monthly_chunks"))
        finally:
            sys.path.remove(str(root))

    def test_split_small_n_currently_can_have_empty_test(self):
        # R3 is now fixed: for n=3..5, test_chunks must be >= 1.
        # The test name is kept for historical traceability.
        time_fold = _import_time_fold_top_level()
        get_train_valid_test_split = time_fold.get_train_valid_test_split

        for n in (3, 4, 5):
            chunks = [{"i": i} for i in range(n)]
            split = get_train_valid_test_split(chunks)
            total = sum(len(split[k]) for k in split)
            self.assertEqual(total, n)
            self.assertGreaterEqual(
                len(split["test_chunks"]), 1,
                f"n={n}: test_chunks should be >= 1 after R3 fix",
            )

    def test_split_n_ge_3_should_have_non_empty_train_valid_test(self):
        # Guardrail for the docstring promise: n>=3 → all three splits non-empty.
        time_fold = _import_time_fold_top_level()
        get_train_valid_test_split = time_fold.get_train_valid_test_split

        for n in range(3, 10):
            chunks = [{"i": i} for i in range(n)]
            split = get_train_valid_test_split(chunks)
            self.assertGreaterEqual(len(split["train_chunks"]), 1)
            self.assertGreaterEqual(len(split["valid_chunks"]), 1)
            self.assertGreaterEqual(
                len(split["test_chunks"]), 1,
                f"n={n} should allocate >=1 test chunk",
            )

    def test_mixed_tzinfo_currently_raises_typeerror(self):
        # R4 is now fixed: mixed tz raises ValueError (not TypeError).
        # The test name is kept for historical traceability.
        time_fold = _import_time_fold_top_level()
        get_monthly_chunks = time_fold.get_monthly_chunks

        aware = datetime(2025, 1, 1, tzinfo=timezone.utc)
        naive = datetime(2025, 2, 1)
        with self.assertRaises(ValueError):
            get_monthly_chunks(aware, naive)

    def test_mixed_tzinfo_should_raise_valueerror_with_clear_message(self):
        # Guardrail: mixed tz must raise ValueError with an informative message.
        time_fold = _import_time_fold_top_level()
        get_monthly_chunks = time_fold.get_monthly_chunks

        aware = datetime(2025, 1, 1, tzinfo=timezone.utc)
        naive = datetime(2025, 2, 1)
        with self.assertRaises(ValueError, msg="Expected ValueError for mixed tz"):
            get_monthly_chunks(aware, naive)

    def test_invalid_fractions_currently_can_produce_empty_splits(self):
        # R5 is now fixed: invalid fractions raise ValueError immediately.
        # The test name is kept for historical traceability.
        time_fold = _import_time_fold_top_level()
        get_train_valid_test_split = time_fold.get_train_valid_test_split

        chunks = [{"i": i} for i in range(10)]
        with self.assertRaises(ValueError):
            get_train_valid_test_split(chunks, train_frac=1.2, valid_frac=0.1)

    def test_invalid_fractions_should_raise_valueerror(self):
        # Guardrail: bad fractions must raise ValueError with a clear message.
        time_fold = _import_time_fold_top_level()
        get_train_valid_test_split = time_fold.get_train_valid_test_split

        chunks = [{"i": i} for i in range(10)]
        with self.assertRaises(ValueError):
            get_train_valid_test_split(chunks, train_frac=1.2, valid_frac=0.1)
