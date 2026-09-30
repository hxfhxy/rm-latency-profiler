"""CLI：inspect（快速看一眼录像）、report（生成 HTML 报告）、export（导出帧表 CSV）。"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from . import analysis, ingest
from . import report as report_mod
from .mapping import MappingError, load_mapping

app = typer.Typer(add_completion=False, help="RM 视觉链路延迟黑盒分析工具")


def _load_mapping_opt(mapping_file: Path | None):
    if mapping_file is None:
        return None
    try:
        return load_mapping(mapping_file)
    except MappingError as exc:
        typer.secho(f"映射文件错误：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc


@app.command()
def inspect(
    path: Path = typer.Argument(exists=True, dir_okay=False, help="MCAP 录像路径"),
    mapping_file: Path | None = typer.Option(None, "--mapping", "-m", help="字段映射 YAML/JSON"),
    as_json: bool = typer.Option(False, "--json", help="输出机器可读 JSON"),
):
    """打印录像概况：通道、帧数、时长、帧率、串口链路状态。"""
    from mcap.reader import make_reader

    mapping = _load_mapping_opt(mapping_file)
    channels = []
    with open(path, "rb") as f:
        summary = make_reader(f).get_summary()
        for chid, ch in summary.channels.items():
            schema = summary.schemas[ch.schema_id]
            channels.append(
                {
                    "topic": ch.topic,
                    "schema": schema.name,
                    "encoding": schema.encoding,
                    "messages": summary.statistics.channel_message_counts.get(chid, 0),
                }
            )

    df = ingest.load_frames(path, mapping)
    stats = analysis.frame_interval_stats(analysis.with_segments(df))
    info = {
        "file": str(path),
        "channels": channels,
        "frames": len(df),
        "duration_s": round(analysis.duration_s(df), 2),
        "fps": round(stats["fps"], 2) if stats["fps"] == stats["fps"] else None,
        "serial_active": analysis.serial_active(df),
    }

    if as_json:
        typer.echo(json.dumps(info, ensure_ascii=False, indent=2))
        return

    typer.echo("通道：")
    for ch in channels:
        typer.echo(f"  {ch['topic']}  [{ch['encoding']}]  {ch['schema']}  {ch['messages']} 条")
    typer.echo(f"\n遥测帧数：{info['frames']}")
    typer.echo(f"时长：{info['duration_s']} s，帧率 ≈ {info['fps']} fps")
    typer.echo(f"串口链路：{'有写出' if info['serial_active'] else '无指令写出'}")


@app.command()
def report(
    paths: list[Path] = typer.Argument(exists=True, dir_okay=False, help="MCAP 录像路径，可多个"),
    out: Path = typer.Option("report", "--out", "-o", help="输出目录"),
    mapping_file: Path | None = typer.Option(None, "--mapping", "-m", help="字段映射 YAML/JSON"),
):
    """为每份录像生成一个单文件 HTML 延迟报告。"""
    mapping = _load_mapping_opt(mapping_file)

    out.mkdir(parents=True, exist_ok=True)
    for path in paths:
        df = analysis.with_segments(ingest.load_frames(path, mapping))
        stats = analysis.frame_interval_stats(df)
        percentiles = analysis.percentile_table(df)
        deltas = analysis.serial_tx_deltas(df)
        attribution = analysis.gap_attribution(df)
        breakdown = analysis.gap_breakdown(df)

        out_file = out / f"{path.stem}-latency-report.html"
        html = report_mod.build_report(df, stats, percentiles, deltas, path,
                                       attribution=attribution, breakdown=breakdown)
        out_file.write_text(html, encoding="utf-8")

        e2e = percentiles.loc["end_to_end"]
        typer.echo(f"{path.name} -> {out_file}")
        typer.echo(
            f"  端到端 capture→finish：p50 {e2e['p50']:.1f} / p95 {e2e['p95']:.1f} / "
            f"p99 {e2e['p99']:.1f} ms"
        )


@app.command()
def export(
    path: Path = typer.Argument(exists=True, dir_okay=False, help="MCAP 录像路径"),
    out: Path = typer.Option(None, "--out", "-o", help="输出 CSV 路径，默认与录像同名"),
    mapping_file: Path | None = typer.Option(None, "--mapping", "-m", help="字段映射 YAML/JSON"),
):
    """把逐帧遥测表导出为 CSV，便于自行分析。"""
    mapping = _load_mapping_opt(mapping_file)
    df = analysis.with_segments(ingest.load_frames(path, mapping))
    out = out or path.with_suffix(".frames.csv")
    df.to_csv(out, index=False)
    typer.echo(f"已导出 {len(df)} 帧 -> {out}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="监听地址；保持本机回环，勿暴露到局域网"),
    port: int = typer.Option(8321, help="监听端口"),
    no_browser: bool = typer.Option(False, "--no-browser", help="不自动打开浏览器"),
    example: Path | None = typer.Option(None, "--example", exists=True, dir_okay=False,
                                        help="提供一份示例录像，界面出现一键加载按钮"),
):
    """启动本地 Web 界面（交互式探索，比静态报告多视野缩放与多录像对比）。"""
    import threading
    import time
    import webbrowser

    import uvicorn

    from .webapp import create_app

    url = f"http://{host}:{port}"
    typer.echo(f"Web 界面：{url}（Ctrl+C 退出）")
    if not no_browser:
        # 等端口真正可连再弹浏览器：绑定失败（端口被旧进程占用）时
        # 不能把用户带去一个残留的旧服务——那正是"界面一直没更新"的来源
        def _open_when_ready():
            import urllib.request

            for _ in range(50):
                try:
                    urllib.request.urlopen(url, timeout=0.2)
                    webbrowser.open(url)
                    return
                except OSError:
                    time.sleep(0.1)
            typer.secho(f"服务未能启动：{url} 无法连接（端口被占用？）",
                        fg=typer.colors.RED, err=True)

        threading.Thread(target=_open_when_ready, daemon=True).start()
    uvicorn.run(create_app(example), host=host, port=port, log_level="warning")


@app.command()
def compare(
    before: Path = typer.Argument(exists=True, dir_okay=False, help="改动前的录像"),
    after: Path = typer.Argument(exists=True, dir_okay=False, help="改动后的录像"),
    out: Path = typer.Option("report", "--out", "-o", help="输出目录"),
    mapping_file: Path | None = typer.Option(None, "--mapping", "-m", help="字段映射 YAML/JSON"),
):
    """生成两份录像的 A/B 对比报告（优化验收用；前提：输入相同、只改被测项）。"""
    from . import compare as compare_mod

    mapping = _load_mapping_opt(mapping_file)
    df_before = analysis.with_segments(ingest.load_frames(before, mapping))
    df_after = analysis.with_segments(ingest.load_frames(after, mapping))

    out.mkdir(parents=True, exist_ok=True)
    out_file = out / f"{before.stem}-vs-{after.stem}-compare.html"
    html = compare_mod.build_compare(df_before, df_after, before, after)
    out_file.write_text(html, encoding="utf-8")

    e2e_before = analysis.percentile_table(df_before).loc["end_to_end"]
    e2e_after = analysis.percentile_table(df_after).loc["end_to_end"]
    typer.echo(f"对比报告已生成：{out_file}")
    typer.echo(
        f"端到端 p50 {e2e_before['p50']:.1f} → {e2e_after['p50']:.1f} ms | "
        f"p99 {e2e_before['p99']:.1f} → {e2e_after['p99']:.1f} ms"
    )


if __name__ == "__main__":
    app()
