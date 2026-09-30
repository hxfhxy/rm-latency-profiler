"""对比报告测试：两份合成录像的 delta 方向与 HTML 内容契约。"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rm_latency import analysis, ingest
from rm_latency import compare as compare_mod
from rm_latency.cli import app

from .synthetic import write_synthetic

runner = CliRunner()


@pytest.fixture(scope="module")
def pair(tmp_path_factory) -> tuple[Path, Path]:
    """优化前 compute=10ms，优化后 compute=7ms；其余构造完全一致。"""
    d = tmp_path_factory.mktemp("cmp")
    before = d / "before.mcap"
    after = d / "after.mcap"
    write_synthetic(before, n_frames=200, compute_ms=10.0, serial_written=True)
    write_synthetic(after, n_frames=200, compute_ms=7.0, serial_written=True)
    return before, after


def test_delta_rows_direction_and_value(pair):
    df_b = analysis.with_segments(ingest.load_frames(pair[0]))
    df_a = analysis.with_segments(ingest.load_frames(pair[1]))
    deltas = compare_mod._delta_rows(df_b, df_a,
                                     analysis.percentile_table(df_b),
                                     analysis.percentile_table(df_a))
    compute_p50 = next(r for r in deltas
                       if r["segment"] == "submit_to_finish" and r["stat"] == "p50")
    assert compute_p50["before"] == pytest.approx(10.0, abs=0.01)
    assert compute_p50["after"] == pytest.approx(7.0, abs=0.01)
    assert compute_p50["delta"] == pytest.approx(-3.0, abs=0.01)
    assert compute_p50["pct"] == pytest.approx(-30.0, abs=0.1)


def test_compare_html_contract(pair, tmp_path):
    out = tmp_path / "cmp.html"
    df_b = analysis.with_segments(ingest.load_frames(pair[0]))
    df_a = analysis.with_segments(ingest.load_frames(pair[1]))
    html = compare_mod.build_compare(df_b, df_a, pair[0], pair[1])
    out.write_text(html, encoding="utf-8")

    assert "js-plotly-plot" in html  # 叠加曲线图渲染
    assert "-30%" in html  # 计算 p50 10→7ms 的 delta 百分比
    assert 'class="good"' in html  # 改善标绿
    assert "Latency Compare" in html
    assert "对比成立前提" in html


def test_cli_compare(tmp_path, pair):
    out_dir = tmp_path / "rep"
    result = runner.invoke(app, ["compare", str(pair[0]), str(pair[1]),
                                 "-o", str(out_dir)])
    assert result.exit_code == 0
    out_file = out_dir / "before-vs-after-compare.html"
    assert out_file.exists()
    text = out_file.read_text(encoding="utf-8")
    assert "端到端 p50" in text


# ---------- 噪声基线（D12） ----------

def test_noise_flags_identical_runs_as_inconclusive(tmp_path):
    """同配置带抖动的两场：delta≈0 必落在噪声带内，结论必须拒绝下结论。"""
    paths = []
    for name in ("run1.mcap", "run2.mcap"):
        path = tmp_path / name
        write_synthetic(path, n_frames=400, compute_ms=10.0, jitter_ms=1.0)
        paths.append(path)
    df1 = analysis.with_segments(ingest.load_frames(paths[0]))
    df2 = analysis.with_segments(ingest.load_frames(paths[1]))
    deltas = compare_mod._delta_rows(df1, df2,
                                     analysis.percentile_table(df1),
                                     analysis.percentile_table(df2))
    e2e_p50 = next(r for r in deltas
                   if r["segment"] == "end_to_end" and r["stat"] == "p50")
    assert e2e_p50["within_noise"] is True
    assert e2e_p50["noise"] > 0

    html = compare_mod.build_compare(df1, df2, paths[0], paths[1])
    assert "不构成结论" in html
    assert "噪声内" in html


def test_noise_does_not_mask_real_change(pair):
    """10→7ms 的真实差异（30%）必须逃出噪声带，不许误标。"""
    df_b = analysis.with_segments(ingest.load_frames(pair[0]))
    df_a = analysis.with_segments(ingest.load_frames(pair[1]))
    deltas = compare_mod._delta_rows(df_b, df_a,
                                     analysis.percentile_table(df_b),
                                     analysis.percentile_table(df_a))
    compute_p50 = next(r for r in deltas
                       if r["segment"] == "submit_to_finish" and r["stat"] == "p50")
    assert compute_p50["within_noise"] is False
    # 尾分位未做检验，明确为 None 而不是误标
    compute_p99 = next(r for r in deltas
                       if r["segment"] == "submit_to_finish" and r["stat"] == "p99")
    assert compute_p99["within_noise"] is None
