"""映射、CLI 与容错测试：异构字段名的跨队适配、端到端命令、缺失字段旧版录像。"""

import json
from pathlib import Path

import numpy as np
import pytest
from mcap.writer import Writer
from typer.testing import CliRunner

from rm_latency import analysis, ingest
from rm_latency.cli import app
from rm_latency.mapping import MappingError, load_mapping

from .synthetic import write_synthetic

runner = CliRunner()


def _write_custom(path: Path, n_frames: int = 30, include_timing: bool = True) -> None:
    """写一份字段命名完全不同的遥测录像（模拟别的队），timing 可选。"""
    schema = b"""{"type":"object","properties":{"snap":{"type":"object"}}}"""
    with open(path, "wb") as f:
        writer = Writer(f)
        writer.start()
        schema_id = writer.register_schema("other.v9", "jsonschema", schema)
        channel_id = writer.register_channel("/other/telemetry", "json", schema_id)
        for i in range(n_frames):
            capture = i * 8_000_000  # 8 ms 周期
            msg = {
                "snap": {
                    "frame_id": i + 1,
                    "t_cam": str(capture),
                    "t_in": str(capture + 2_000_000),
                    "t_out": str(capture + 2_000_000 + 5_000_000),
                }
            }
            if not include_timing:
                del msg["snap"]["t_in"]
                del msg["snap"]["t_out"]
            writer.add_message(channel_id, log_time=capture, publish_time=capture,
                               data=json.dumps(msg).encode())
        writer.finish()


@pytest.fixture(scope="module")
def custom_mcap(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("custom") / "other-team.mcap"
    _write_custom(path)
    return path


@pytest.fixture(scope="module")
def mapping_file(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("custom") / "other-team.yaml"
    path.write_text(
        "fingerprint: \"snap\"\n"
        "fields:\n"
        "  seq: snap.frame_id\n"
        "  stamp_ns: snap.t_cam\n"
        "  capture_ns: snap.t_cam\n"
        "  submit_ns: snap.t_in\n"
        "  finish_ns: snap.t_out\n",
        encoding="utf-8",
    )
    return path


def test_mapping_loads_and_defaults_fill(mapping_file):
    mapping = load_mapping(mapping_file)
    assert mapping.fingerprint == "snap"
    assert mapping.paths["written"] == "serial_tx.written"  # 未覆盖的回落默认


def test_mapping_rejects_unknown_canonical(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("fields:\n  nonsense: a.b\n", encoding="utf-8")
    with pytest.raises(MappingError, match="未知规范字段"):
        load_mapping(path)


def test_custom_mapping_reads_renamed_fields(custom_mcap, mapping_file):
    mapping = load_mapping(mapping_file)
    df = analysis.with_segments(ingest.load_frames(custom_mcap, mapping))
    assert len(df) == 30
    assert df["seq"].iloc[0] == 1
    # 已知构造：传输 2 ms、计算 5 ms
    assert df["capture_to_submit_ms"].median() == pytest.approx(2.0)
    assert df["submit_to_finish_ms"].median() == pytest.approx(5.0)


def test_missing_timing_fields_tolerated(tmp_path):
    """旧版录像缺 timing 字段：行保留、值为 NaN、统计不崩。"""
    path = tmp_path / "old.mcap"
    _write_custom(path, n_frames=10, include_timing=False)
    mapping = load_mapping(_shared_mapping(tmp_path))
    df = ingest.load_frames(path, mapping)
    assert len(df) == 10
    assert df["submit_ns"].isna().all()
    table = analysis.percentile_table(analysis.with_segments(df))
    assert np.isnan(table.loc["end_to_end", "p50"])


def _shared_mapping(tmp_path: Path) -> Path:
    path = tmp_path / "mapping.yaml"
    path.write_text(
        "fingerprint: \"snap\"\n"
        "fields:\n"
        "  seq: snap.frame_id\n"
        "  stamp_ns: snap.t_cam\n"
        "  capture_ns: snap.t_cam\n"
        "  submit_ns: snap.t_in\n"
        "  finish_ns: snap.t_out\n",
        encoding="utf-8",
    )
    return path


def test_cli_inspect_json(tmp_path):
    path = tmp_path / "s.mcap"
    write_synthetic(path, n_frames=40, serial_written=True)
    result = runner.invoke(app, ["inspect", str(path), "--json"])
    assert result.exit_code == 0
    info = json.loads(result.output)
    assert info["frames"] == 40
    assert info["serial_active"] is True


def test_cli_report_and_export(tmp_path):
    path = tmp_path / "s.mcap"
    write_synthetic(path, n_frames=60, serial_written=True)
    out_dir = tmp_path / "rep"

    result = runner.invoke(app, ["report", str(path), "-o", str(out_dir)])
    assert result.exit_code == 0
    html = (out_dir / "s-latency-report.html").read_text(encoding="utf-8")
    assert html.count("plotly-graph-div") == 4  # 时序 + 直方 + 间隔 + 串口
    assert "p50" in html

    result = runner.invoke(app, ["export", str(path), "-o", str(tmp_path / "frames.csv")])
    assert result.exit_code == 0
    csv_text = (tmp_path / "frames.csv").read_text(encoding="utf-8")
    assert "end_to_end_ms" in csv_text


def test_cli_report_custom_mapping(tmp_path, custom_mcap, mapping_file):
    out_dir = tmp_path / "rep"
    result = runner.invoke(
        app, ["report", str(custom_mcap), "-o", str(out_dir), "-m", str(mapping_file)]
    )
    assert result.exit_code == 0
    assert (out_dir / "other-team-latency-report.html").exists()


def test_cli_report_bad_mapping_fails(tmp_path):
    path = tmp_path / "s.mcap"
    write_synthetic(path, n_frames=5)
    bad = tmp_path / "bad.yaml"
    bad.write_text("fields:\n  nonsense: x.y\n", encoding="utf-8")
    result = runner.invoke(app, ["report", str(path), "-m", str(bad)])
    assert result.exit_code == 2
