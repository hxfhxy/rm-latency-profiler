"""A/B 对比报告：两份录像的三段延迟 delta 表 + 对齐叠加曲线。

对比成立的前提（报告里注明）：两次录像的输入相同（replay 同一段视频），
除被测改动外配置一致——否则 delta 没有归因意义。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

from .analysis import duration_s, frame_interval_stats, percentile_table

# 时序叠加抽稀阈值，与单报告一致
_MAX_POINTS = 5000

_DELTA_STATS = ("p50", "p95", "p99", "max")
_SEGMENTS = (
    ("capture_to_submit", "capture→submit（传输）"),
    ("submit_to_finish", "submit→finish（计算）"),
    ("end_to_end", "端到端"),
)


def build_compare(df_before: pd.DataFrame, df_after: pd.DataFrame,
                  before_path: Path, after_path: Path) -> str:
    """组装对比 HTML。df_* 均应为 with_segments 之后的帧表。"""
    pct_before = percentile_table(df_before)
    pct_after = percentile_table(df_after)
    stats_before = frame_interval_stats(df_before)
    stats_after = frame_interval_stats(df_after)
    deltas = _delta_rows(pct_before, pct_after)

    summary = [
        ("对比对象", f"{before_path.name} → {after_path.name}"),
        ("帧数", f"{len(df_before)} → {len(df_after)}"),
        ("时长", f"{duration_s(df_before):.1f} s → {duration_s(df_after):.1f} s"),
        ("帧率", f"{stats_before['fps']:.1f} → {stats_after['fps']:.1f} fps"),
        ("结论", _conclusion(deltas)),
    ]

    body = (
        _delta_table_html(deltas)
        + _overlay_figure_html(df_before, df_after, before_path, after_path)
        + _attribution_html(df_before, df_after, before_path.name, after_path.name)
    )
    from .report import assemble_html

    return assemble_html(
        title=f"Latency Compare — {before_path.stem} vs {after_path.stem}",
        summary_rows=summary,
        body=body,
        note="对比成立前提：两次录像输入相同（如 replay 同一段视频），除被测改动外配置一致。"
             "delta = after − before；延迟指标越低越好，绿色为改善、红色为恶化。"
             "掉帧归因为启发式判据（D11），不是证明。",
    )


def _delta_rows(pct_before: pd.DataFrame, pct_after: pd.DataFrame) -> list[dict]:
    rows = []
    for seg_key, seg_label in _SEGMENTS:
        for stat in _DELTA_STATS:
            before = float(pct_before.loc[seg_key, stat])
            after = float(pct_after.loc[seg_key, stat])
            delta = after - before
            pct = (delta / before * 100) if before > 0 else float("nan")
            rows.append({
                "segment": seg_key, "segment_label": seg_label, "stat": stat,
                "before": before, "after": after, "delta": delta, "pct": pct,
            })
    return rows


def _conclusion(deltas: list[dict]) -> str:
    e2e = {r["stat"]: r for r in deltas if r["segment"] == "end_to_end"}
    p50, p99 = e2e["p50"], e2e["p99"]
    fmt = lambda r: f"{r['delta']:+.1f} ms ({r['pct']:+.0f}%)"  # noqa: E731
    return f"端到端 p50 {fmt(p50)}，p99 {fmt(p99)}"


def _delta_table_html(deltas: list[dict]) -> str:
    rows = []
    for r in deltas:
        if r["pct"] != r["pct"]:  # NaN
            change = '<span class="muted">—</span>'
        else:
            cls = "good" if r["delta"] < 0 else ("bad" if r["delta"] > 0 else "")
            change = (f'<span class="{cls}">{r["after"]:.1f} ms'
                      f'（{r["pct"]:+.0f}%）</span>')
        rows.append(
            f"<tr><th>{r['segment_label']}</th><td>{r['stat']}</td>"
            f'<td class="num">{r["before"]:.1f}</td>{change}</tr>'
        )
    return (
        "<h2>Delta 表（before → after）</h2>"
        '<table><tr><th>分段</th><th>分位</th><th>before ms</th><th>after</th></tr>'
        + "".join(rows) + "</table>"
    )


def _overlay_figure_html(df_before: pd.DataFrame, df_after: pd.DataFrame,
                         before_path: Path, after_path: Path) -> str:
    from plotly.io import to_html

    fig = go.Figure()
    for df, name, color in ((df_before, before_path.name, "#636efa"),
                            (df_after, after_path.name, "#ef553b")):
        stride = max(1, len(df) // _MAX_POINTS)
        view = df.iloc[::stride]
        t = (view["stamp_ns"].to_numpy() - df["stamp_ns"].iloc[0]) / 1e9
        fig.add_trace(go.Scatter(
            x=t, y=view["end_to_end_ms"], name=name, mode="lines",
            line={"color": color, "width": 1},
            hovertemplate="%{y:.2f} ms @ %{x:.2f}s<extra>" + name + "</extra>",
        ))
    fig.update_layout(
        template="plotly_white", margin={"l": 60, "r": 30, "t": 50, "b": 50},
        height=360, xaxis_title="运行时间 (s)", yaxis_title="端到端 ms",
        title="端到端延迟叠加（各自相对起点对齐）",
    )
    return (
        "<h2>时序叠加</h2>"
        + to_html(fig, include_plotlyjs=True, full_html=False,
                  config={"displaylogo": False, "responsive": True})
    )


def _attribution_html(df_before: pd.DataFrame, df_after: pd.DataFrame,
                      name_before: str, name_after: str) -> str:
    from .analysis import gap_attribution

    a, b = gap_attribution(df_before), gap_attribution(df_after)

    def fmt(d: dict) -> str:
        return (f"{d['compute_overload']} / 采集侧 {d['capture_side']}"
                f" / 未归因 {d['unknown']}")
    return (
        "<h2>掉帧归因</h2>"
        f'<table><tr><th>录像</th><th>计算过载 / 采集侧 / 未归因（次）</th></tr>'
        f"<tr><th>{name_before}</th><td>{fmt(a)}</td></tr>"
        f"<tr><th>{name_after}</th><td>{fmt(b)}</td></tr></table>"
    )
