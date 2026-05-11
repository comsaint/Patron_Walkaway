"""High-tier patron MVP：ingest → features → train → serving（骨架，待接線）。

建議從子模組匯入具體符號，避免 `python -m trainer_high_tier.run_mvp` 與套件載入衝突：

    from trainer_high_tier.run_mvp import HighTierMvpConfig, run_end_to_end
"""
