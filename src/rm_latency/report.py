"""HTML 报告生成：plotly 图表 + 摘要表，单文件离线可打开。

plotly.js 整包内联进 HTML（约 3MB）：目标用户是赛场边没有稳定外网的队员，
离线可打开是硬需求，不是体积优化问题。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.io import to_html

# 时序曲线降采样阈值：超过该点数按等步长抽稀，浏览器渲染才不卡
_MAX_TIMELINE_POINTS = 5000

_FIG_LAYOUT = {
    "template": "plotly_white",
    "margin": {"l": 60, "r": 30, "t": 50, "b": 50},
    "height": 340,
}


def build_report(df: pd.DataFrame, stats: dict, percentiles: pd.DataFrame,
                 serial_deltas: pd.DataFrame | None, source: Path) -> str:
    """组装完整 HTML 报告字符串。"""
    figures: list[go.Figure] = [
        _timeline_figure(df),
        _histogram_figure(df),
        _interval_figure(df, stats),
    ]
    if serial_deltas is not None and serial_active_any(serial_deltas):
        figures.append(_serial_figure(df, serial_deltas))

    divs: list[str] = []
    for i, fig in enumerate(figures):
        divs.append(
            to_html(
                fig,
                include_plotlyjs=(True if i == 0 else False),
                full_html=False,
                config={"displaylogo": False, "responsive": True},
            )
        )

    return _assemble_html(
        title=f"Latency Report — {source.name}",
        summary_rows=_summary_rows(df, stats, percentiles, serial_deltas),
        body="\n".join(divs),
    )


def serial_active_any(serial_deltas: pd.DataFrame) -> bool:
    return bool(serial_deltas[["queued_delta", "written_delta"]].to_numpy().sum() > 0)


def _timeline_figure(df: pd.DataFrame) -> go.Figure:
    """三段延迟全程时序；超过阈值抽稀，抽稀比例写进 hover 供读者知情。"""
    n = len(df)
    stride = max(1, n // _MAX_TIMELINE_POINTS)
    view = df.iloc[::stride]
    t = view["stamp_ns"].to_numpy() / 1e9
    t -= t[0]  # 相对秒数，避免绝对单调时钟暴露机器开机时间

    fig = go.Figure()
    for col, label, color in (
        ("capture_to_submit_ms", "capture→submit（传输）", "#636efa"),
        ("submit_to_finish_ms", "submit→finish（计算）", "#ef553b"),
        ("end_to_end_ms", "端到端 capture→finish", "#444"),
    ):
        fig.add_trace(
            go.Scatter(
                x=t, y=view[col], name=label, mode="lines", line={"color": color, "width": 1},
                hovertemplate="%{y:.2f} ms @ %{x:.2f}s<extra>" + label + "</extra>",
            )
        )
    if stride > 1:
        fig.add_annotation(
            text=f"抽稀显示 1/{stride}（共 {n} 帧）", xref="paper", yref="paper",
            x=1, y=1.15, showarrow=False, font={"size": 11, "color": "#888"},
        )
    fig.update_layout(title="延迟时序（全程）", xaxis_title="运行时间 (s)",
                      yaxis_title="ms", **_FIG_LAYOUT)
    return fig


def _histogram_figure(df: pd.DataFrame) -> go.Figure:
    """三段延迟分布直方图，共用 x 轴便于对比量级。"""
    fig = go.Figure()
    for col, label in (
        ("capture_to_submit_ms", "capture→submit"),
        ("submit_to_finish_ms", "submit→finish"),
        ("end_to_end_ms", "端到端"),
    ):
        values = df[col].dropna()
        fig.add_trace(go.Histogram(x=values, name=label, opacity=0.65,
                                   nbinsx=int(np.clip(len(values) / 20, 20, 120))))
    p = percentile_table_for_axis(df)
    fig.update_layout(title="延迟分布（p50/p95/p99 虚线为端到端）",
                      xaxis_title="ms", yaxis_title="帧数", barmode="overlay", **_FIG_LAYOUT)
    for q, dash in ((50, "dot"), (95, "dash"), (99, "solid")):
        fig.add_vline(x=p[q], line_dash=dash, line_color="#333",
                      annotation_text=f"p{q}={p[q]:.1f}", annotation_font_size=10)
    return fig


def percentile_table_for_axis(df: pd.DataFrame) -> dict:
    from .analysis import percentile_table

    row = percentile_table(df).loc["end_to_end"]
    return {int(q[1:]): row[q] for q in ("p50", "p95", "p99")}


def _interval_figure(df: pd.DataFrame, stats: dict) -> go.Figure:
    """帧间隔直方图 + 掉帧标记。"""
    intervals = df["interval_ms"].dropna()
    median = float(intervals.median()) if len(intervals) else 0
    clip = median * 5 if median > 0 else None
    shown = intervals[intervals <= clip] if clip else intervals

    fig = go.Figure(go.Histogram(x=shown, nbinsx=80, name="帧间隔"))
    fig.add_vline(x=median, line_color="#ef553b", annotation_text=f"中位 {median:.2f} ms",
                  annotation_font_size=10)
    fig.update_layout(
        title=f"帧间隔分布（fps≈{stats['fps']:.1f}，抖动≈{stats['jitter_ms']:.2f} ms，"
              f"疑似掉帧 {stats['gaps']} 次，间隔>2×中位）",
        xaxis_title="ms", yaxis_title="帧数", **_FIG_LAYOUT,
    )
    return fig


def _serial_figure(df: pd.DataFrame, serial_deltas: pd.DataFrame) -> go.Figure:
    """串口下行每帧增量时序（仅当链路工作过才生成）。"""
    t = df["stamp_ns"].to_numpy() / 1e9
    t -= t[0]
    fig = go.Figure()
    for col, label in (("queued_delta", "入队"), ("written_delta", "写出"),
                       ("queue_drops_delta", "队列丢弃"), ("write_failures_delta", "写失败")):
        fig.add_trace(go.Scatter(x=t, y=serial_deltas[col], name=label, mode="lines",
                                 line={"width": 1}))
    fig.update_layout(title="串口指令下行（每帧增量）", xaxis_title="运行时间 (s)",
                      yaxis_title="条/帧", **_FIG_LAYOUT)
    return fig


def _summary_rows(df: pd.DataFrame, stats: dict, percentiles: pd.DataFrame,
                  serial_deltas: pd.DataFrame | None) -> list[tuple[str, str]]:
    e2e = percentiles.loc["end_to_end"]
    rows = [
        ("帧数", f"{len(df)}"),
        ("时长", f"{(df['stamp_ns'].iloc[-1] - df['stamp_ns'].iloc[0]) / 1e9:.1f} s"),
        ("帧率", f"{stats['fps']:.1f} fps（抖动 ±{stats['jitter_ms']:.2f} ms）"),
        ("端到端 capture→finish",
         f"p50 {e2e['p50']:.1f} / p95 {e2e['p95']:.1f} / p99 {e2e['p99']:.1f} / "
         f"max {e2e['max']:.1f} ms"),
        ("疑似掉帧", f"{stats['gaps']} 次，累计 {stats.get('gap_total_ms', 0):.0f} ms"),
    ]
    if serial_deltas is None or not serial_active_any(serial_deltas):
        rows.append(("串口下行", "整场无指令写出（未接下位机或指令被上游拦截）"))
    return rows


def _assemble_html(title: str, summary_rows: list[tuple[str, str]], body: str) -> str:
    summary_html = "".join(
        f"<tr><td>{k}</td><td><b>{v}</b></td></tr>" for k, v in summary_rows
    )
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: system-ui, "PingFang SC", "Microsoft YaHei", sans-serif;
         max-width: 1080px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
  h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.1rem; margin-top: 2rem; }}
  table {{ border-collapse: collapse; margin: 1rem 0; }}
  td, th {{ border: 1px solid #ddd; padding: 0.4rem 0.9rem; font-size: 0.92rem; }}
  td:first-child {{ color: #666; }}
  .note {{ color: #888; font-size: 0.85rem; }}
</style>
</head>
<body>
<h1>{title}</h1>
<h2>摘要</h2>
<table>{summary_html}</table>
<p class="note">分段含义：capture→submit = 图像从采集回调到任务提交（传输/排队）；
submit→finish = 本帧检测+预测+解算（计算）；端到端不含串口写出与下位机执行。
分位数为全程聚合；时序图定位异常发生的时刻。</p>
{body}
</body>
</html>"""
