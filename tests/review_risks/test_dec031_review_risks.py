"""DEC-031 / T-DEC031 Code Review — 風險點轉為可執行守衛（tests-only）。

對應 `.cursor/plans/STATUS.md`「Code Review：DEC-031 / T-DEC031 步驟 1–2」所列項目。
僅新增測試與靜態契約；**不修改 production code**。

實作刻意**不** `import trainer.trainer`（該模組匯入過重，pytest 收集階段易逾時）；
亦**不**頂層 `import pandas`（部分環境首次 import 極慢）。
改以 `ast.get_source_segment` 讀取 repo 內之 `.py` 原始檔擷取函式本文。

若未來 production 修正某風險，相關測試可能需更新預期（由失敗提示契約變更）。
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest

import numpy as np

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _read_top_level_function(rel_path: str, func_name: str) -> str:
    path = _REPO_ROOT / rel_path
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            seg = ast.get_source_segment(text, node)
            if not seg:
                raise AssertionError(f"empty segment for {func_name} in {rel_path}")
            return seg
    raise AssertionError(f"top-level function {func_name!r} not found in {rel_path}")


def _process_chunk_source_llm_to_labels() -> str:
    """Return process_chunk source from Track LLM call through label section marker."""
    src = _read_top_level_function("trainer/training/trainer.py", "process_chunk")
    start = src.find("_bets_llm_result = compute_track_llm_features")
    assert start != -1, "process_chunk must assign compute_track_llm_features result"
    end = src.find("# --- Labels (C1 extended pull)", start)
    assert end != -1, "process_chunk must retain Labels section marker after Track LLM"
    return src[start:end]


class TestDec031Risk01PartialLlmColumnsMerge(unittest.TestCase):
    """Review #1: 運算未報錯但 result 缺候選欄時，仍以「存在即 merge」過濾。"""

    def test_process_chunk_filters_llm_columns_by_presence_in_result(self):
        """契約：目前實作以 `fid in _bets_llm_result.columns` 決定 merge 子集（靜默缺欄風險）。"""
        seg = _process_chunk_source_llm_to_labels()
        self.assertIn(
            "if fid and fid in _bets_llm_result.columns",
            seg,
            "Expected partial-column filter; if removed, revisit DEC-031 column contract.",
        )


class TestDec031Risk02Float32IntegerPrecision(unittest.TestCase):
    """Review #2: float32 無法精確表示所有大整數（COUNT 等）。"""

    def test_float32_collapses_adjacent_large_integers(self):
        """2^24+1 與 2^24 在 float32 下可相等 — 重現大計數舍入風險。"""
        a = np.float32(16777217)
        b = np.float32(16777216)
        self.assertEqual(a, b, "numpy float32 should round 16777217 to representable value")


class TestDec031Risk03ObjectDtypeSkippedByNumericGuard(unittest.TestCase):
    """Review #3: DuckDB→pandas 之 object／decimal 常使 is_numeric_dtype 為 False，cast 迴圈可略過。

    刻意不在此檔 `import pandas`：部分環境首次 import pandas 極慢，會讓 pytest 收集卡住。
    行為層面可由整合測試 mock DuckDB 回傳 object 欄補強；此處以 production 原始碼契約守門。
    """

    def test_compute_track_llm_cast_guards_with_is_numeric_dtype(self):
        src = _read_top_level_function("trainer/features/features.py", "compute_track_llm_features")
        self.assertIn("is_numeric_dtype", src)
        self.assertIn("astype(np.float32)", src)


class TestDec031Risk04NoSwallowBetweenLlmAndLabels(unittest.TestCase):
    """Review #4: trainer 在 Track LLM 與 labels 之間不得再有吞例外（DEC-031）。"""

    def test_no_except_keyword_in_llm_to_labels_segment(self):
        seg = _process_chunk_source_llm_to_labels()
        self.assertIsNone(
            re.search(r"\bexcept\b", seg),
            "DEC-031: no `except` between compute_track_llm_features and compute_labels",
        )


class TestDec031Risk05Float32HalvesStorageVsFloat64(unittest.TestCase):
    """Review #5: float32 僅減半元素大小；merge 尖峰仍可能存在（理論守衛）。"""

    def test_float32_itemsize_is_half_of_float64(self):
        self.assertEqual(np.dtype(np.float32).itemsize, 4)
        self.assertEqual(np.dtype(np.float64).itemsize, 8)


class TestDec031Risk06ScorerBacktesterDegradePaths(unittest.TestCase):
    """Review #6: scorer／backtester 與 trainer fail-fast 語意不一致（文件化契約）。"""

    def test_scorer_wraps_track_llm_in_try_except(self):
        src = _read_top_level_function("trainer/serving/scorer.py", "score_once")
        idx = src.find("compute_track_llm_features(")
        self.assertNotEqual(idx, -1)
        # 區塊含多行 log，窗口需涵蓋至 `except Exception as exc:`
        window = src[max(0, idx - 400) : idx + 900]
        self.assertIn("try:", window)
        self.assertRegex(window, r"except\s+Exception")

    def test_backtester_sets_track_llm_degraded_on_failure(self):
        src = _read_top_level_function("trainer/training/backtester.py", "backtest")
        self.assertIn("_track_llm_degraded = True", src)
        self.assertIn("except Exception as exc:", src)


class TestDec031Risk07LoggingWhenFeatureSpecNonNull(unittest.TestCase):
    """Review #7: feature_spec 非空時即會記錄 Track LLM computed（與是否有候選／merge 無關）。"""

    def test_success_log_after_merge_block_not_nested_in_feature_cols_if(self):
        seg = _process_chunk_source_llm_to_labels()
        self.assertIn("Track LLM computed", seg)
        merge_if = seg.find('if _bets_llm_feature_cols and "bet_id"')
        log_pos = seg.find("Track LLM computed")
        self.assertNotEqual(merge_if, -1)
        self.assertGreater(log_pos, merge_if, "success log should follow merge-if block")


class TestDec031Risk08ComputeEarlyExitNoCandidates(unittest.TestCase):
    """與 Review #7 相關：spec 無 candidates 時 compute 早退仍會被 process_chunk 呼叫。"""

    def test_compute_warns_when_track_llm_has_no_candidates(self):
        src = _read_top_level_function("trainer/features/features.py", "compute_track_llm_features")
        self.assertIn(
            "track_llm has no candidates",
            src,
            "Early exit path should remain observable for empty candidate lists",
        )


if __name__ == "__main__":
    unittest.main()
