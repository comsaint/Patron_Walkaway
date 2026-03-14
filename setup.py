from setuptools import setup

# 項目 2.1：子包須列舉以確保安裝後 walkaway_ml.core 等可 import（STATUS Code Review 項目 2.1 §2）
# 項目 2.4：entry points 對應 python -m trainer.* 薄層轉發（安裝後為 walkaway_ml）
setup(
    packages=[
        "walkaway_ml",
        "walkaway_ml.scripts",
        "walkaway_ml.core",
        "walkaway_ml.features",
        "walkaway_ml.training",
        "walkaway_ml.serving",
        "walkaway_ml.etl",
    ],
    package_dir={"walkaway_ml": "trainer", "walkaway_ml.scripts": "trainer/scripts"},
    entry_points={
        "console_scripts": [
            "walkaway-train=walkaway_ml.trainer:main",
            "walkaway-backtester=walkaway_ml.backtester:main",
            "walkaway-scorer=walkaway_ml.scorer:main",
            "walkaway-validator=walkaway_ml.validator:main",
            "walkaway-status=walkaway_ml.status_server:main",
            "walkaway-api=walkaway_ml.api_server:run",
        ],
    },
)
