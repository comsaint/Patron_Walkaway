"""自 YAML 載入 baseline run 設定（F1／F2）。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Mapping

import yaml


@dataclass(frozen=True)
class BaselineRunConfig:
    """單次 baseline smoke／full run 的設定子集。"""

    raw: Mapping[str, Any]
    config_path: Path

    @property
    def experiment_id(self) -> str | None:
        """實驗識別；可為 ``None``（smoke 應改為明確字串）。"""
        v = self.raw.get("experiment_id")
        return str(v) if v is not None else None

    @property
    def data_source_kind(self) -> str:
        """``synthetic_smoke`` 或 ``parquet``。"""
        ds = self.raw.get("data_source") or {}
        return str(ds.get("kind") or "synthetic_smoke")

    @property
    def data_source_path(self) -> str | None:
        """Parquet 檔路徑（相對於設定檔目錄或絕對路徑）。"""
        ds = self.raw.get("data_source") or {}
        p = ds.get("path_or_uri")
        return str(p) if p else None

    @property
    def column_event_time(self) -> str:
        """事件時間欄（時序切分與窗過濾）。"""
        cols = self.raw.get("columns") or {}
        return str(cols.get("event_time") or "bet_time")

    @property
    def column_label(self) -> str:
        """二元標籤欄。"""
        cols = self.raw.get("columns") or {}
        return str(cols.get("label") or "label")

    @property
    def column_censored(self) -> str:
        """Censored 布林欄。"""
        cols = self.raw.get("columns") or {}
        return str(cols.get("censored") or "censored")

    @property
    def column_score(self) -> str | None:
        """可選：預先計分之排序欄（無則 smoke 用序號分數）。"""
        cols = self.raw.get("columns") or {}
        s = cols.get("score")
        return str(s) if s else None

    @property
    def split_train_frac(self) -> float:
        """訓練段比例（其餘為 valid+test；禁 shuffle）。"""
        sp = self.raw.get("split") or {}
        return float(sp.get("train_frac", 0.6))

    @property
    def split_valid_frac(self) -> float:
        """驗證段占「非訓練」部分比例。"""
        sp = self.raw.get("split") or {}
        return float(sp.get("valid_frac", 0.5))

    @property
    def split_protocol_name(self) -> str:
        """寫入 metrics／run_state 之切分協定名。"""
        meta = self.raw.get("metadata") or {}
        root = self.raw.get("split_protocol")
        return str(meta.get("split_protocol") or root or "temporal_slice_no_shuffle")

    @property
    def feature_set_version(self) -> str | None:
        """特徵集版本字串。"""
        meta = self.raw.get("metadata") or {}
        v = meta.get("feature_set_version", self.raw.get("feature_set_version"))
        return str(v) if v is not None else None

    @property
    def label_contract_version(self) -> str | None:
        """標籤契約版本。"""
        meta = self.raw.get("metadata") or {}
        v = meta.get("label_contract_version", self.raw.get("label_contract_version"))
        return str(v) if v is not None else None

    @property
    def data_window(self) -> Mapping[str, Any] | None:
        """``data_window: {start, end}`` 原样；可為空。"""
        dw = self.raw.get("data_window")
        return dw if isinstance(dw, Mapping) else None

    @property
    def threshold_min_alert_count(self) -> int:
        """DEC-026 選阈用之最小 alerts（smoke 建議 1）。"""
        ts = self.raw.get("threshold_selection") or {}
        return int(ts.get("min_alert_count", 1))

    @property
    def threshold_recall_floor(self) -> float | None:
        """Recall 下限；``None`` 表示不限制。"""
        ts = self.raw.get("threshold_selection") or {}
        v = ts.get("recall_floor", 0.01)
        if v is None:
            return None
        return float(v)

    @property
    def threshold_min_alerts_per_hour(self) -> float | None:
        """可選：每小時最小 alerts。"""
        ts = self.raw.get("threshold_selection") or {}
        v = ts.get("min_alerts_per_hour")
        return float(v) if v is not None else None

    @property
    def threshold_window_hours(self) -> float | None:
        """可選：評估窗長（小時），供 per-hour 約束。"""
        ts = self.raw.get("threshold_selection") or {}
        v = ts.get("window_hours")
        return float(v) if v is not None else None

    @property
    def tier0_r1_enabled(self) -> bool:
        """是否評估 Tier-0 R1（pace）規則列。"""
        t0 = self.raw.get("tier0") or {}
        r1 = t0.get("r1") or {}
        return bool(r1.get("enabled", False))

    @property
    def tier0_r1_signals(self) -> list[str]:
        """R1 訊號欄名列表（例如 ``pace_drop_ratio``）。"""
        t0 = self.raw.get("tier0") or {}
        r1 = t0.get("r1") or {}
        sigs = r1.get("signals")
        if not sigs:
            return []
        if isinstance(sigs, str):
            return [str(sigs)]
        return [str(x) for x in sigs]

    @property
    def tier0_r2_enabled(self) -> bool:
        """是否評估 Tier-0 R2（loss：net 與 wager 各一列）。"""
        t0 = self.raw.get("tier0") or {}
        r2 = t0.get("r2") or {}
        return bool(r2.get("enabled", False))

    @property
    def tier0_r2_net_column(self) -> str:
        """R2 net proxy 來源欄名。"""
        t0 = self.raw.get("tier0") or {}
        r2 = t0.get("r2") or {}
        return str(r2.get("net_column", "loss_proxy_net"))

    @property
    def tier0_r2_wager_column(self) -> str:
        """R2 wager proxy 來源欄名。"""
        t0 = self.raw.get("tier0") or {}
        r2 = t0.get("r2") or {}
        return str(r2.get("wager_column", "loss_proxy_wager"))

    @property
    def tier0_r3_enabled(self) -> bool:
        """是否評估 Tier-0 R3（ADT／theo 比例）。"""
        t0 = self.raw.get("tier0") or {}
        r3 = t0.get("r3") or {}
        return bool(r3.get("enabled", False))

    @property
    def tier0_r3_variants(self) -> list[str]:
        """R3 變體列表：``adt30``、``adt180``、``theo_per_session``。"""
        t0 = self.raw.get("tier0") or {}
        r3 = t0.get("r3") or {}
        v = r3.get("variants")
        if not v:
            return []
        if isinstance(v, str):
            return [str(v)]
        return [str(x) for x in v]

    @property
    def tier0_r3_current_session_theo_column(self) -> str:
        """本場 theo 欄名（分子）。"""
        t0 = self.raw.get("tier0") or {}
        r3 = t0.get("r3") or {}
        return str(r3.get("current_session_theo_column", "current_session_theo"))

    @property
    def tier0_r3_tau_grid(self) -> list[float]:
        """R3 分母敏感度 ``tau`` 掃描點（>0）；缺省或空列表時為 ``[1.0]``（等同未掃描）。"""
        t0 = self.raw.get("tier0") or {}
        r3 = t0.get("r3") or {}
        tg = r3.get("tau_grid")
        if tg is None:
            return [1.0]
        if isinstance(tg, (int, float)):
            t = float(tg)
            if not math.isfinite(t) or t <= 0.0:
                raise ValueError(f"tier0.r3.tau_grid 須為正有限數，收到: {tg!r}")
            return [t]
        if isinstance(tg, list):
            if len(tg) == 0:
                return [1.0]
            out: list[float] = []
            for i, x in enumerate(tg):
                t = float(x)
                if not math.isfinite(t) or t <= 0.0:
                    raise ValueError(
                        f"tier0.r3.tau_grid[{i}] 須為正有限數，收到: {x!r}"
                    )
                out.append(t)
            return out
        raise ValueError(
            f"tier0.r3.tau_grid 必須為 list 或單一數值，收到: {type(tg).__name__}"
        )

    @property
    def tier1_m1_enabled(self) -> bool:
        """是否訓練並評估 M1（LogisticRegression）。"""
        t1 = self.raw.get("tier1") or {}
        m1 = t1.get("m1") or {}
        return bool(m1.get("enabled", False))

    @property
    def tier1_m1_feature_columns(self) -> list[str]:
        """M1 特徵欄名列表（須存在於 train／test）。"""
        t1 = self.raw.get("tier1") or {}
        m1 = t1.get("m1") or {}
        cols = m1.get("feature_columns")
        if not cols:
            return []
        if isinstance(cols, str):
            return [str(cols)]
        return [str(x) for x in cols]

    @property
    def tier1_m1_max_iter(self) -> int:
        """M1 ``LogisticRegression.max_iter``。"""
        t1 = self.raw.get("tier1") or {}
        m1 = t1.get("m1") or {}
        return int(m1.get("max_iter", 500))

    @property
    def tier1_m2_enabled(self) -> bool:
        """是否訓練並評估 M2（SGDClassifier）。"""
        t1 = self.raw.get("tier1") or {}
        m2 = t1.get("m2") or {}
        return bool(m2.get("enabled", False))

    @property
    def tier1_m2_feature_columns(self) -> list[str]:
        """M2 特徵欄名列表（須存在於 train／test）。"""
        t1 = self.raw.get("tier1") or {}
        m2 = t1.get("m2") or {}
        cols = m2.get("feature_columns")
        if not cols:
            return []
        if isinstance(cols, str):
            return [str(cols)]
        return [str(x) for x in cols]

    @property
    def tier1_m2_max_iter(self) -> int:
        """M2 ``SGDClassifier.max_iter``（epochs／passes 上限）。"""
        t1 = self.raw.get("tier1") or {}
        m2 = t1.get("m2") or {}
        return int(m2.get("max_iter", 1000))

    @property
    def tier1_s1_enabled(self) -> bool:
        """是否評估 S1（單特徵排名，無訓練）。"""
        t1 = self.raw.get("tier1") or {}
        s1 = t1.get("s1") or {}
        return bool(s1.get("enabled", False))

    @property
    def tier1_s1_rankings(self) -> list[tuple[str, bool, str | None]]:
        """S1 排名設定：``(欄名, high_is_risk, proxy_type 或 None)`` 有序列表。"""
        t1 = self.raw.get("tier1") or {}
        s1 = t1.get("s1") or {}
        rk = s1.get("rankings")
        if not rk:
            return []
        if not isinstance(rk, list):
            raise ValueError(
                f"tier1.s1.rankings 必須為 list，收到: {type(rk).__name__}"
            )
        out: list[tuple[str, bool, str | None]] = []
        for i, item in enumerate(rk):
            if not isinstance(item, Mapping):
                raise ValueError(
                    f"tier1.s1.rankings[{i}] 必須為 mapping，收到: {type(item).__name__}"
                )
            if "column" not in item:
                raise ValueError(f"tier1.s1.rankings[{i}] 缺少鍵 'column'")
            col = str(item["column"])
            hir = item.get("high_is_risk", True)
            if not isinstance(hir, bool):
                hir = bool(hir)
            pt_raw = item.get("proxy_type", None)
            proxy: str | None
            if pt_raw is None or pt_raw == "":
                proxy = None
            else:
                proxy = str(pt_raw)
            out.append((col, hir, proxy))
        return out

    @property
    def reference_model_enabled(self) -> bool:
        """是否載入同窗 ``training_metrics.json``（Phase D／SSOT §8）。"""
        rm = self.raw.get("reference_model") or {}
        return bool(rm.get("enabled", False))

    @property
    def reference_model_training_metrics_path(self) -> str | None:
        """同窗 JSON 路徑（相對倉庫根或絕對路徑）。"""
        rm = self.raw.get("reference_model") or {}
        p = rm.get("training_metrics_path")
        if p is None or str(p).strip() == "":
            return None
        return str(p).strip()

    @property
    def reference_model_metrics_section(self) -> str:
        """``training_metrics.json`` 頂層區段鍵（常見 ``rated``、``model_default``）。"""
        rm = self.raw.get("reference_model") or {}
        return str(rm.get("metrics_section", "rated"))


def _repo_root_from_config_path(config_path: Path) -> Path:
    """倉庫根目錄（``…/baseline_models/config/<file>`` 之上兩層）。"""
    return config_path.resolve().parents[2]


def _apply_alignment_fragment(data: dict, fragment: Mapping[str, Any]) -> None:
    """將單一 alignment mapping 合入 ``data`` 的 ``data_window``／``split``。"""
    dw = fragment.get("data_window")
    if isinstance(dw, Mapping):
        merged = dict(data.get("data_window") or {})
        for k in ("start", "end"):
            if k in dw:
                merged[k] = dw[k]
        data["data_window"] = merged
    sp = fragment.get("split")
    if isinstance(sp, Mapping):
        s0 = dict(data.get("split") or {})
        for k in ("train_frac", "valid_frac"):
            if k in sp and sp[k] is not None:
                s0[k] = sp[k]
        data["split"] = s0


def merge_training_provenance_into_raw(data: Mapping[str, Any], config_path: Path) -> None:
    """若啟用，自 ``training_metrics.json``／``training_provenance.json`` 合併對齊資訊。

    合併順序（後者覆蓋前者）：先讀 ``training_metrics.json`` 頂層
    ``baseline_data_alignment``（trainer 新產物會寫入）；再讀同目錄
    ``training_provenance.json``（可选手改覆蓋）。

    用於與該次 LightGBM 訓練相同之 ``data_window`` 與列切分（``valid_frac`` 為 train
    之後剩餘列中的比例；對應 trainer 之 ``VALID_SPLIT_FRAC/(1-TRAIN_SPLIT_FRAC)``）。

    Args:
        data: YAML 根 mapping（可變）。
        config_path: 目前設定檔路徑。

    Raises:
        ValueError: ``training_provenance.json`` 根非 mapping。
    """
    if not isinstance(data, dict):
        return
    ref = data.get("reference_model")
    if not isinstance(ref, dict):
        return
    if not bool(ref.get("apply_training_provenance", False)):
        return
    root = _repo_root_from_config_path(config_path)
    metrics_rel = ref.get("training_metrics_path")
    if not metrics_rel or not str(metrics_rel).strip():
        return
    metrics_path = (root / str(metrics_rel).strip()).resolve()
    if metrics_path.is_file():
        try:
            tm = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            tm = {}
        bda = tm.get("baseline_data_alignment")
        if isinstance(bda, Mapping):
            _apply_alignment_fragment(data, bda)

    prov_rel = ref.get("training_provenance_path")
    if prov_rel and str(prov_rel).strip():
        prov_path = (root / str(prov_rel).strip()).resolve()
    else:
        prov_path = metrics_path.parent / "training_provenance.json"
    if not prov_path.is_file():
        return
    loaded = json.loads(prov_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, Mapping):
        raise ValueError(
            f"training_provenance 根節點必須為 mapping，收到: {type(loaded)!r}"
        )
    _apply_alignment_fragment(data, dict(loaded))


def load_run_config_yaml(path: str | Path) -> BaselineRunConfig:
    """讀取 YAML 並包成 :class:`BaselineRunConfig`（缺檔 fail-fast）。

    Args:
        path: 設定檔路徑。

    Returns:
        凍結設定物件。

    Raises:
        FileNotFoundError: 檔案不存在。
        ValueError: YAML 無法解析或根節點非 mapping。
    """
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"baseline config 不存在: {p!r}")
    text = p.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, Mapping):
        raise ValueError(f"baseline config 根節點必須為 mapping，收到: {type(data)!r}")
    if isinstance(data, dict):
        merge_training_provenance_into_raw(data, p)
    return BaselineRunConfig(raw=data, config_path=p)
