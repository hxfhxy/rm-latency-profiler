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
    # 升级防坑：页面不许吃浏览器缓存
    assert res.headers["cache-control"] == "no-cache"


def test_version_endpoint(client):
    res = client.get("/api/version")
    assert res.status_code == 200
    assert res.json()["version"].count(".") == 2  # 语义化版本号


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
    assert len(data["gap_causes"]) == len(data["gap_indices"])
    # 合成录像时间戳均匀：无掉帧，归因总数为 0
    assert data["gap_attribution"]["total"] == 0


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


# ---------- 视频回放 ----------

@pytest.fixture(scope="module")
def video_mcap_bytes(tmp_path_factory) -> bytes:
    path = tmp_path_factory.mktemp("web") / "video.mcap"
    write_synthetic(path, n_frames=50, images=True)
    return path.read_bytes()


def test_analyze_extracts_video(client, video_mcap_bytes):
    res = client.post(
        "/api/analyze",
        files={"file": ("video.mcap", io.BytesIO(video_mcap_bytes), "application/octet-stream")},
    )
    assert res.status_code == 200
    video = res.json()["video"]
    assert video is not None
    assert video["count"] == 50
    # 图像与遥测同 log_time 约定写入：对齐后首帧应在 t≈0
    assert video["t"][0] == pytest.approx(0.0, abs=0.01)
    assert video["fps"] == pytest.approx(200.0, rel=0.01)  # 5ms 周期


def test_frame_endpoint_serves_jpeg(client, video_mcap_bytes):
    analyze = client.post(
        "/api/analyze",
        files={"file": ("video.mcap", io.BytesIO(video_mcap_bytes), "application/octet-stream")},
    )
    run_id = analyze.json()["video"]["run_id"]

    res = client.get(f"/api/frame/{run_id}/7")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert res.content.startswith(b"\xff\xd8")  # JPEG magic
    assert "immutable" in res.headers["cache-control"]

    assert client.get(f"/api/frame/{run_id}/9999").status_code == 404
    assert client.get("/api/frame/999999/0").status_code == 404


def test_analyze_without_image_channel_has_no_video(client, mcap_bytes):
    res = client.post(
        "/api/analyze",
        files={"file": ("synth.mcap", io.BytesIO(mcap_bytes), "application/octet-stream")},
    )
    assert res.status_code == 200
    assert res.json()["video"] is None


# ---------- 串口下行面板 ----------

def test_analyze_serial_bucket_series(client, mcap_bytes):
    res = client.post(
        "/api/analyze",
        files={"file": ("synth.mcap", io.BytesIO(mcap_bytes), "application/octet-stream")},
    )
    s = res.json()["serial"]
    assert s is not None and s["active"] is True
    assert s["bucket_s"] == 1
    # 每帧 1 条：100 帧 → 99 条增量（首帧 diff 为 0），分桶后总和守恒
    assert sum(s["queued_per_s"]) == N_FRAMES - 1
    assert sum(s["written_per_s"]) == N_FRAMES - 1
    assert sum(s["drops_per_s"]) == 0
    assert s["totals"]["written"] == N_FRAMES - 1
    assert len(s["t"]) == len(s["written_per_s"])


def test_analyze_without_serial_has_no_serial_data(client, video_mcap_bytes):
    res = client.post(
        "/api/analyze",
        files={"file": ("video.mcap", io.BytesIO(video_mcap_bytes), "application/octet-stream")},
    )
    assert res.status_code == 200
    assert res.json()["serial"] is None


def test_video_downsample_covers_full_duration(client, tmp_path):
    """帧数超上限：等步长抽稀必须覆盖完整时间轴（D10：不许截断尾部）。"""
    path = tmp_path / "many.mcap"
    write_synthetic(path, n_frames=5000, images=True)  # 5ms 周期 = 25s
    res = client.post(
        "/api/analyze",
        files={"file": ("many.mcap", io.BytesIO(path.read_bytes()), "application/octet-stream")},
    )
    assert res.status_code == 200
    video = res.json()["video"]
    assert video["downsampled"] is True
    assert video["stride"] == 3  # ceil(5000/2400)
    assert video["count"] == (5000 + 2) // 3
    assert video["t"][-1] == pytest.approx(24.995, abs=0.1)  # 尾部仍在
