"""延迟分析：段统计、分位数、帧间隔与掉帧、串口增量。

全部时间计算用帧头单调时钟域（stamp_ns），log_time 仅作绝对时间参考。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 帧间隔超过中位数的 1.5 倍判为疑似掉帧。
# 不能用 2.0：恰好丢一帧产生精确 2× 中位数的间隔，严格大于永远抓不到；
# 1.5 倍在正常抖动（<<周期 25%）下不误报，单帧掉落必命中。
GAP_FACTOR = 1.5

_SEGMENTS = {
    "capture_to_submit": ("capture_ns", "submit_ns"),
    "submit_to_finish": ("submit_ns", "finish_ns"),
    "end_to_end": ("capture_ns", "finish_ns"),
}


def with_segments(df: pd.DataFrame) -> pd.DataFrame:
    """派生三段延迟列（ms）与帧间隔列，返回副本。"""
    out = df.copy()
    for name, (start, end) in _SEGMENTS.items():
        out[f"{name}_ms"] = (out[end] - out[start]) / 1e6
    out["interval_ms"] = out["stamp_ns"].diff() / 1e6
    return out


def percentile_table(df: pd.DataFrame) -> pd.DataFrame:
    """三段延迟的 p50/p95/p99/max 摘要表，单位 ms。"""
    rows = []
    for name in _SEGMENTS:
        series = df[f"{name}_ms"].dropna()
        rows.append(
            {
                "segment": name,
                "p50": _percentile(series, 50),
                "p95": _percentile(series, 95),
                "p99": _percentile(series, 99),
                "max": series.max() if len(series) else float("nan"),
                "mean": series.mean() if len(series) else float("nan"),
            }
        )
    return pd.DataFrame(rows).set_index("segment")


def _percentile(series: pd.Series, q: float) -> float:
    return float(np.percentile(series, q)) if len(series) else float("nan")


def frame_interval_stats(df: pd.DataFrame) -> dict:
    """帧率与抖动摘要；gap 判据 = 间隔 > GAP_FACTOR × 中位数且 > 1 ms。"""
    intervals = df["interval_ms"].dropna()
    if intervals.empty:
        return {"fps": float("nan"), "jitter_ms": float("nan"), "gaps": 0}

    median = float(intervals.median())
    gap_mask = intervals > max(GAP_FACTOR * median, 1.0)
    return {
        "fps": 1000.0 / median if median > 0 else float("nan"),
        # 抖动用对中位数的平均绝对偏差：比标准差抗毛刺
        "jitter_ms": float((intervals - median).abs().mean()),
        "gaps": int(gap_mask.sum()),
        "gap_total_ms": float(intervals[gap_mask].sum()),
    }


def median_noise_ms(series: pd.Series) -> float:
    """样本中位数的 95% 噪声带宽（±ms）：2 × 1.2533 × robust σ / √n。

    1.2533 是正态假设下中位数标准误对 σ/√n 的修正系数；robust σ = 1.4826 × MAD。
    用途：compare 报告判断 delta 是否落在统计噪声内（D12）。
    样本 < 30 时不可信，返回 NaN。
    """
    s = pd.Series(series).dropna()
    n = len(s)
    if n < 30:
        return float("nan")
    sigma = 1.4826 * float((s - s.median()).abs().median())
    return 2 * 1.2533 * sigma / (n ** 0.5)


def gap_attribution(df: pd.DataFrame) -> dict:
    """把疑似掉帧归因到最可能的原因（启发式，诚实标注为归因而非证明）。

    判据：空洞出现在第 i 帧与第 i-1 帧之间时，看第 i-1 帧的计算耗时
    （submit→finish）是否超过稳态帧周期（间隔中位数）：
    - 超周期 → 计算过载：管线还在算上一帧，新帧被丢（常见于丢新语义的有界队列）
    - 未超 → 采集侧断流：上一帧算得过来，是相机/传输没把帧送进来
    """
    causes = {"compute_overload": 0, "capture_side": 0, "unknown": 0}
    intervals = df["interval_ms"]
    median = float(intervals.median()) if len(intervals) else 0.0
    if median <= 0:
        return {**causes, "total": 0}

    compute = df["submit_to_finish_ms"]
    for i in np.flatnonzero((intervals > max(GAP_FACTOR * median, 1.0)).to_numpy()):
        if i == 0 or pd.isna(compute.iloc[i - 1]):
            causes["unknown"] += 1
        elif float(compute.iloc[i - 1]) > median:
            causes["compute_overload"] += 1
        else:
            causes["capture_side"] += 1
    total = causes["compute_overload"] + causes["capture_side"] + causes["unknown"]
    return {**causes, "total": int(total)}


def serial_tx_deltas(df: pd.DataFrame) -> pd.DataFrame:
    """累计串口计数 -> 每帧增量；计数器回退（设备重启）按 0 处理并计入 resets。"""
    out = pd.DataFrame(index=df.index)
    for col in ("queued", "queue_drops", "written", "write_failures"):
        diff = df[col].diff()
        out[f"{col}_delta"] = diff.clip(lower=0).fillna(0).astype("int64")
    out["counter_resets"] = (df[["queued"]].diff() < 0).any(axis=1)
    return out


def serial_active(df: pd.DataFrame) -> bool:
    """整场录像里串口链路是否真的工作过（全 0 = 未接设备，报告需注明）。"""
    return bool((df["written"].fillna(0) > 0).any())


def duration_s(df: pd.DataFrame) -> float:
    """录像时长（秒），按首尾帧的单调时钟差。"""
    return float((df["stamp_ns"].iloc[-1] - df["stamp_ns"].iloc[0]) / 1e9)
