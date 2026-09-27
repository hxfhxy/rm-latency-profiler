"""摄取与分析的核心测试：全部基于合成录像，期望值手工可算。"""

from pathlib import Path

import numpy as np
import pytest

from rm_latency import analysis, ingest

from .synthetic import write_synthetic

N_FRAMES = 100
PERIOD = 5.0
TRANSPORT = 1.0
COMPUTE = 10.0


@pytest.fixture(scope="module")
def mcap_file(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("synth") / "synth.mcap"
    write_synthetic(path, n_frames=N_FRAMES, period_ms=PERIOD,
                    transport_ms=TRANSPORT, compute_ms=COMPUTE, serial_written=True)
    return path


@pytest.fixture(scope="module")
def df(mcap_file):
    return analysis.with_segments(ingest.load_frames(mcap_file))


def test_load_frames_shape_and_order(df):
    assert len(df) == N_FRAMES
    assert df["stamp_ns"].is_monotonic_increasing
    # 纳秒字符串必须被转成 int64 而不是留在 object 列
    assert df["capture_ns"].dtype == np.int64


def test_segment_values_match_construction(df):
    # 常数段 + 每 25 帧一个 3 倍毛刺：p50 应等于常数
    assert df["capture_to_submit_ms"].median() == pytest.approx(TRANSPORT)
    assert df["submit_to_finish_ms"].median() == pytest.approx(COMPUTE)
    assert df["end_to_end_ms"].median() == pytest.approx(TRANSPORT + COMPUTE)
    # 毛刺帧确实存在
    assert (df["capture_to_submit_ms"] > 2 * TRANSPORT).sum() == N_FRAMES // 25


def test_percentile_table(df):
    table = analysis.percentile_table(df)
    e2e = table.loc["end_to_end"]
    assert e2e["p50"] == pytest.approx(TRANSPORT + COMPUTE, abs=1e-6)
    # max 是毛刺帧：(3*1 + 3*10) = 33
    assert e2e["max"] == pytest.approx(33.0, rel=1e-3)


def test_frame_interval_stats(df):
    stats = analysis.frame_interval_stats(df)
    assert stats["fps"] == pytest.approx(1000.0 / PERIOD, rel=1e-3)
    assert stats["gaps"] == 0  # 均匀间隔，无掉帧


def test_gap_detection(tmp_path):
    # 真实掉帧的形态是"少一帧"：删掉一行后相邻间隔变为 2× 周期
    path = tmp_path / "gap.mcap"
    write_synthetic(path, n_frames=50)
    df = ingest.load_frames(path)
    df = df.drop(index=20).reset_index(drop=True)
    stats = analysis.frame_interval_stats(analysis.with_segments(df))
    assert stats["gaps"] == 1


def test_serial_deltas(df):
    deltas = analysis.serial_tx_deltas(df)
    assert deltas["written_delta"].sum() == N_FRAMES - 1  # 首帧 diff 为 NaN->0
    assert deltas["queue_drops_delta"].sum() == 0
    assert analysis.serial_active(df)


def test_serial_inactive_detected(tmp_path):
    path = tmp_path / "noserial.mcap"
    write_synthetic(path, n_frames=20, serial_written=False)
    df = ingest.load_frames(path)
    assert not analysis.serial_active(df)


def test_telemetry_channel_discovery(mcap_file):
    cid, topic = ingest.find_telemetry_channel(mcap_file)
    assert topic == "/ace/debug/auto_aim"
    assert cid == 1  # 合成文件里唯一的通道
