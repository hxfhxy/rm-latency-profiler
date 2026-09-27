"""Web 界面测试：API 契约用合成录像验证，页面可达性检查。"""

import io

import pytest
from fastapi.testclient import TestClient

from rm_latency.webapp import create_app

from .synthetic import write_synthetic

N_FRAMES = 100


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    return TestClient(create_app())


@pytest.fixture(scope="module")
def mcap_bytes(tmp_path_factory) -> bytes:
    path = tmp_path_factory.mktemp("web") / "synth.mcap"
    write_synthetic(path, n_frames=N_FRAMES, serial_written=True)
    return path.read_bytes()


def test_index_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "rm-latency-profiler" in res.text
    assert "plotly.min.js" in res.text


def test_plotly_js_served_offline(client):
    res = client.get("/static/plotly.min.js")
    assert res.status_code == 200
    assert len(res.content) > 100_000  # 整包 plotly.js，不是占位


def test_analyze_returns_expected_stats(client, mcap_bytes):
    res = client.post(
        "/api/analyze",
        files={"file": ("synth.mcap", io.BytesIO(mcap_bytes), "application/octet-stream")},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["file"] == "synth.mcap"
    assert data["frames"] == N_FRAMES
    assert data["fps"] == pytest.approx(1000 / 5.0, abs=0.1)
    assert data["segments"]["end_to_end"]["p50"] == pytest.approx(11.0, abs=0.01)
    # 前端重算分位数需要逐帧数组
    assert len(data["timeline"]["t"]) == N_FRAMES
    assert len(data["timeline"]["end_to_end_ms"]) == N_FRAMES
    assert isinstance(data["gap_indices"], list)


def test_analyze_downsamples_large_input(client, tmp_path):
    path = tmp_path / "big.mcap"
    write_synthetic(path, n_frames=60_000, serial_written=False)
    res = client.post(
        "/api/analyze",
        files={"file": ("big.mcap", io.BytesIO(path.read_bytes()), "application/octet-stream")},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["frames"] == 60_000
    assert data["downsampled"] is True
    assert len(data["timeline"]["t"]) <= 50_000
    # 统计仍基于全量：p50 不受抽稀影响
    assert data["segments"]["end_to_end"]["p50"] == pytest.approx(11.0, abs=0.01)


def test_analyze_rejects_non_telemetry_mcap(client):
    res = client.post(
        "/api/analyze",
        files={"file": ("bad.mcap", io.BytesIO(b"not an mcap"), "application/octet-stream")},
    )
    assert res.status_code == 422
    assert "MCAP" in res.json()["detail"]


def test_analyze_rejects_bad_mapping(client, mcap_bytes, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("fields:\n  nonsense: x.y\n", encoding="utf-8")
    res = client.post(
        "/api/analyze",
        files={
            "file": ("synth.mcap", io.BytesIO(mcap_bytes), "application/octet-stream"),
            "mapping": ("bad.yaml", io.BytesIO(bad.read_bytes()), "application/octet-stream"),
        },
    )
    assert res.status_code == 422
    assert "映射文件错误" in res.json()["detail"]
