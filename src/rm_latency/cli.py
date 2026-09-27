"""CLI：inspect（快速看一眼录像）与 report（生成 HTML 报告）。"""

from __future__ import annotations

from pathlib import Path

import typer

from . import analysis, ingest
from . import report as report_mod

app = typer.Typer(add_completion=False, help="RM 视觉链路延迟黑盒分析工具")


@app.command()
def inspect(path: Path = typer.Argument(exists=True, dir_okay=False, help="MCAP 录像路径")):
    """打印录像概况：通道、帧数、时间范围、检测到的遥测通道。"""

    from mcap.reader import make_reader

    with open(path, "rb") as f:
        reader = make_reader(f)
        summary = reader.get_summary()
        typer.echo("通道：")
        for chid, ch in summary.channels.items():
            schema = summary.schemas[ch.schema_id]
            count = summary.statistics.channel_message_counts.get(chid, 0)
            typer.echo(f"  {ch.topic}  [{schema.encoding}]  {schema.name}  {count} 条")

    df = ingest.load_frames(path)
    stats = analysis.frame_interval_stats(analysis.with_segments(df))
    typer.echo(f"\n遥测帧数：{len(df)}")
    typer.echo(f"时长：{analysis.duration_s(df):.1f} s，帧率 ≈ {stats['fps']:.1f} fps")
    typer.echo(f"串口链路：{'有写出' if analysis.serial_active(df) else '无指令写出'}")


@app.command()
def report(
    path: Path = typer.Argument(exists=True, dir_okay=False, help="MCAP 录像路径"),
    out: Path = typer.Option("report", "--out", "-o", help="输出目录"),
):
    """生成单文件 HTML 延迟报告。"""
    df = analysis.with_segments(ingest.load_frames(path))
    stats = analysis.frame_interval_stats(df)
    percentiles = analysis.percentile_table(df)
    deltas = analysis.serial_tx_deltas(df)

    out.mkdir(parents=True, exist_ok=True)
    out_file = out / f"{path.stem}-latency-report.html"
    html = report_mod.build_report(df, stats, percentiles, deltas, path)
    out_file.write_text(html, encoding="utf-8")

    e2e = percentiles.loc["end_to_end"]
    typer.echo(f"报告已生成：{out_file}")
    typer.echo(
        f"端到端 capture→finish：p50 {e2e['p50']:.1f} / p95 {e2e['p95']:.1f} / "
        f"p99 {e2e['p99']:.1f} ms"
    )


if __name__ == "__main__":
    app()
