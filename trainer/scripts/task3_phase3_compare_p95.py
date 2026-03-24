from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Dict, List


_PERF_LINE_RE = re.compile(r"\[scorer\]\[perf\]\s+top_hotspots:\s*(?P<body>.+)")
_STAGE_RE = re.compile(
    r"(?P<stage>[a-zA-Z0-9_]+)=(?P<sec>\d+(?:\.\d+)?)s\s+"
    r"\(p50=(?P<p50>\d+(?:\.\d+)?)s,\s+p95=(?P<p95>\d+(?:\.\d+)?)s,\s+n=(?P<n>\d+)\)"
)


@dataclass
class StageSample:
    sec: float
    p50: float
    p95: float
    n: int


def _collect_stage_samples(log_path: Path) -> Dict[str, List[StageSample]]:
    stage_samples: Dict[str, List[StageSample]] = {}
    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            m = _PERF_LINE_RE.search(line)
            if not m:
                continue
            body = m.group("body")
            for sm in _STAGE_RE.finditer(body):
                stage = sm.group("stage")
                sample = StageSample(
                    sec=float(sm.group("sec")),
                    p50=float(sm.group("p50")),
                    p95=float(sm.group("p95")),
                    n=int(sm.group("n")),
                )
                stage_samples.setdefault(stage, []).append(sample)
    return stage_samples


def _summary(samples: List[StageSample]) -> Dict[str, float]:
    if not samples:
        return {"count": 0.0, "median_current_sec": 0.0, "median_p50_sec": 0.0, "median_p95_sec": 0.0}
    return {
        "count": float(len(samples)),
        "median_current_sec": float(median([x.sec for x in samples])),
        "median_p50_sec": float(median([x.p50 for x in samples])),
        "median_p95_sec": float(median([x.p95 for x in samples])),
    }


def compare_logs(baseline_log: Path, candidate_log: Path) -> Dict[str, Dict[str, float]]:
    baseline = _collect_stage_samples(baseline_log)
    candidate = _collect_stage_samples(candidate_log)
    all_stages = sorted(set(baseline) | set(candidate))
    report: Dict[str, Dict[str, float]] = {}
    for stage in all_stages:
        b = _summary(baseline.get(stage, []))
        c = _summary(candidate.get(stage, []))
        b_p95 = b["median_p95_sec"]
        c_p95 = c["median_p95_sec"]
        improvement = ((b_p95 - c_p95) / b_p95 * 100.0) if b_p95 > 0 else 0.0
        report[stage] = {
            "baseline_count": b["count"],
            "candidate_count": c["count"],
            "baseline_median_p95_sec": b_p95,
            "candidate_median_p95_sec": c_p95,
            "p95_improvement_pct": improvement,
            "baseline_median_current_sec": b["median_current_sec"],
            "candidate_median_current_sec": c["median_current_sec"],
        }
    return report


def _main() -> None:
    parser = argparse.ArgumentParser(description="Compare scorer p95 hotspots from two log files.")
    parser.add_argument("--baseline-log", required=True, type=Path)
    parser.add_argument("--candidate-log", required=True, type=Path)
    parser.add_argument("--out-json", required=False, type=Path)
    args = parser.parse_args()

    result = compare_logs(args.baseline_log, args.candidate_log)
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))

    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(
            json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )


if __name__ == "__main__":
    _main()
