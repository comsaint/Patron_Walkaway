"""Parallel MVP runner for layered LDA (does not modify ``pipelines.layered_data_assets``).

Subprocesses call existing ``scripts/preprocess_bet_v1.py`` and materializers; outputs live under
``data/parallel_lda_mvp/`` with ``gaming_ym=YYYY-MM`` layout.
"""
