"""本地 Web 界面后端：FastAPI 分析服务。

设计决策（D9/D10）：本地 Web 而非桌面 GUI——分析逻辑零复制复用 ingest/analysis，
浏览器是分析工具的行业形态，且规避无 sudo 环境的 Qt/打包问题。
视频回放走逐帧 JPEG 接口而非 MP4 转码：与延迟时序共用同一时间轴，
帧级对齐是复盘的核心价值，转码做不到。

安全边界：默认只绑定 127.0.0.1，这是单人本机工具，不是网络服务。
运行注册表持有已分析录像的图像字节（有上限、LRU 淘汰），进程退出即释放。
"""

from __future__ import annotations

import tempfile
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from . import analysis, ingest
from .mapping import MappingError, load_mapping

# 时序数组下发上限：超过则等步长抽稀（前端图表渲染上限，分析统计仍用全量）
_MAX_POINTS = 50_000
# 视频帧缓存上限：帧数限制，超出按顺序截断（复盘优先看前段）
_MAX_VIDEO_FRAMES = 2_400
_MAX_RUNS = 3

_STATIC_DIR = Path(__file__).parent / "static"
_IMAGE_SCHEMA = "foxglove.CompressedImage"


class _RunStore:
    """已分析录像的会话内注册表：帧字节缓存 + LRU 淘汰。"""

    def __init__(self) -> None:
        self._runs: OrderedDict[int, dict] = OrderedDict()
        self._next_id = 1

    def put(self, frames: list[bytes], times: list[float], media_type: str) -> int:
        run_id = self._next_id
        self._next_id += 1
        self._runs[run_id] = {
            "frames": frames, "times": times, "media_type": media_type,
        }
        while len(self._runs) > _MAX_RUNS:
            self._runs.popitem(last=False)
        return run_id

    def frame(self, run_id: int, index: int) -> tuple[bytes, str]:
        try:
            run = self._runs[run_id]
        except KeyError:
            raise HTTPException(status_code=404, detail="录像会话已过期，请重新上传") from None
        if not 0 <= index < len(run["frames"]):
            raise HTTPException(status_code=404, detail=f"帧号越界：{index}")
        return run["frames"][index], run["media_type"]


_RUNS = _RunStore()


def create_app(example_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="rm-latency-profiler", docs_url=None, redoc_url=None)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    @app.api_route("/example.mcap", methods=["GET", "HEAD"])
    def example() -> FileResponse:
        # 演示/面试场景一键加载示例数据；未提供 --example 时前端不显示入口。
        # HEAD 供前端探测入口是否显示（FastAPI 的 get() 不含 HEAD）。
        if example_path is None or not example_path.exists():
            raise HTTPException(status_code=404, detail="未提供示例数据")
        return FileResponse(example_path, media_type="application/octet-stream")

    @app.get("/static/plotly.min.js")
    def plotly_js() -> Response:
        # plotly python 包自带完整 plotly.min.js：离线原则（D4）在 Web 界面同样生效
        import plotly.offline as offline

        return Response(offline.get_plotlyjs(), media_type="application/javascript")

    @app.post("/api/analyze")
    async def analyze(
        file: UploadFile = File(...),
        mapping: UploadFile | None = File(None),
    ) -> JSONResponse:
        """上传一份 MCAP（可选映射文件），返回摘要 + 全量时序数据 + 视频元数据。

        分位数在前端对缩放区间实时重算，因此必须下发逐帧数组而非预聚合。
        """
        suffix = Path(file.filename or "run.mcap").suffix or ".mcap"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)
        try:
            mapping_obj = None
            if mapping is not None and mapping.filename:
                map_suffix = Path(mapping.filename).suffix.lower()
                with tempfile.NamedTemporaryFile(
                    suffix=map_suffix, delete=False
                ) as map_tmp:
                    map_tmp.write(await mapping.read())
                    map_path = Path(map_tmp.name)
                try:
                    mapping_obj = load_mapping(map_path)
                except MappingError as exc:
                    raise HTTPException(status_code=422, detail=f"映射文件错误：{exc}") from exc
                finally:
                    map_path.unlink(missing_ok=True)

            try:
                df = analysis.with_segments(ingest.load_frames(tmp_path, mapping_obj))
            except ingest.TelemetryNotFoundError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            video = _extract_video(tmp_path, df)
        finally:
            tmp_path.unlink(missing_ok=True)

        stats = analysis.frame_interval_stats(df)
        percentiles = analysis.percentile_table(df)

        return JSONResponse(
            {
                "file": file.filename,
                "frames": len(df),
                "downsampled": _stride(len(df)) > 1,
                "fps": _finite_or_none(stats["fps"]),
                "jitter_ms": _finite_or_none(stats["jitter_ms"]),
                "gaps": int(stats["gaps"]),
                "serial_active": analysis.serial_active(df),
                "segments": {
                    name: {k: _finite_or_none(row[k]) for k in ("p50", "p95", "p99", "max", "mean")}
                    for name, row in percentiles.iterrows()
                },
                "timeline": _timeline_payload(df),
                "gap_indices": [i for i, _ in _gap_marks(df)],
                "gap_causes": [c for _, c in _gap_marks(df)],
                "gap_attribution": analysis.gap_attribution(df),
                "gap_breakdown": analysis.gap_breakdown(df),
                "video": video,
            }
        )

    @app.get("/api/frame/{run_id}/{index}")
    def frame(run_id: int, index: int) -> Response:
        data, media_type = _RUNS.frame(run_id, index)
        return Response(
            content=data,
            media_type=media_type,
            headers={"Cache-Control": "immutable, max-age=86400"},
        )

    return app


def _extract_video(path: Path, df) -> dict | None:
    """提取图像通道到会话缓存，返回视频元数据；没有图像通道返回 None。

    超过帧数上限时按等步长抽稀覆盖**全时段**（复盘时间轴必须完整，
    精度损失换完整性），而不是从头截断——截断会让时间轴尾部点不出画面。
    总帧数直接读 MCAP summary 的通道统计，省一遍全文件扫描。

    时间对齐：图像消息的 log_time 是墙钟域，遥测 stamp_ns 是运行时单调域。
    两个域的偏移取遥测消息的 median(log_time - stamp)（同一时刻记录，偏移近似恒定），
    图像帧时间 = img_log_time - offset，再换算到与时间轴一致的相对秒。
    """
    info = _find_image_info(path)
    if info is None:
        return None
    topic, total = info

    try:
        offsets = (df["log_time_ns"] - df["stamp_ns"]).dropna()
        align_offset = float(offsets.median()) if len(offsets) else 0.0
    except KeyError:
        align_offset = 0.0

    from foxglove_schemas_protobuf.CompressedImage_pb2 import CompressedImage
    from mcap.reader import make_reader

    if total is None:
        # 极少数录像没有 summary 统计：退回全文件数一遍
        with open(path, "rb") as f:
            total = sum(1 for _ in make_reader(f).iter_messages(topics=[topic]))
    if total == 0:
        return None
    stride = max(1, -(-total // _MAX_VIDEO_FRAMES))  # ceil 除法

    frames: list[bytes] = []
    stamps: list[int] = []
    media_type = "image/jpeg"
    with open(path, "rb") as f:
        for i, (_schema, _channel, message) in enumerate(
            make_reader(f).iter_messages(topics=[topic])
        ):
            if i % stride != 0:
                continue
            img = CompressedImage.FromString(message.data)
            media_type = f"image/{img.format or 'jpeg'}"
            frames.append(img.data)
            stamps.append(message.log_time)

    first_stamp = float(df["stamp_ns"].iloc[0])
    times = [round((s - align_offset - first_stamp) / 1e9, 4) for s in stamps]
    run_id = _RUNS.put(frames, times, media_type)

    # 播放帧率用图像帧自身的间隔中位数，别假设与遥测同帧率
    ivals = np.diff(times)
    fps = round(1.0 / float(np.median(ivals)), 2) if len(ivals) and np.median(ivals) > 0 else None
    return {
        "run_id": run_id,
        "count": len(frames),
        "stride": stride,
        "downsampled": stride > 1,
        "fps": fps,
        "t": times,
    }


def _find_image_info(path: Path) -> tuple[str, int | None] | None:
    """从 summary 找图像通道，返回 (topic, 消息总数)；无图像通道返回 None。

    消息总数来自 summary 的通道统计——避免为定抽稀步长再扫一遍全文件。
    """
    from mcap.reader import make_reader

    with open(path, "rb") as f:
        summary = make_reader(f).get_summary()
        if summary is None:
            return None
        for channel in summary.channels.values():
            schema = summary.schemas.get(channel.schema_id)
            if schema is None or schema.name != _IMAGE_SCHEMA:
                continue
            count = None
            if summary.statistics is not None:
                count = summary.statistics.channel_message_counts.get(channel.id)
            return channel.topic, count
    return None


def _stride(n: int) -> int:
    import math

    return max(1, math.ceil(n / _MAX_POINTS))


def _gap_marks(df) -> list[tuple[int, str]]:
    """疑似掉帧行号 + 归因（compute_overload / capture_side / unknown）。"""
    intervals = df["interval_ms"]
    median = float(intervals.median()) if len(intervals) else 0.0
    if median <= 0:
        return []
    compute = df["submit_to_finish_ms"]
    marks = []
    for i in np.flatnonzero((intervals > max(analysis.GAP_FACTOR * median, 1.0)).to_numpy()):
        if i == 0 or pd.isna(compute.iloc[i - 1]):
            marks.append((int(i), "unknown"))
        elif float(compute.iloc[i - 1]) > median:
            marks.append((int(i), "compute_overload"))
        else:
            marks.append((int(i), "capture_side"))
    return marks


def _timeline_payload(df) -> dict:
    stride = _stride(len(df))
    view = df.iloc[::stride]
    t = (view["stamp_ns"].to_numpy() - df["stamp_ns"].iloc[0]) / 1e9
    marks = _gap_marks(df)
    payload = {
        "t": [round(float(v), 4) for v in t],
        "gap_indices": [i // stride for i, _ in marks],
        "gap_causes": [c for _, c in marks],
    }
    for col in ("capture_to_submit_ms", "submit_to_finish_ms", "end_to_end_ms"):
        payload[col] = [_finite_or_none(v, ndigits=4) for v in view[col]]
    return payload


def _finite_or_none(value, ndigits: int = 2):
    """NaN/Inf -> None（JSON 不支持 NaN，浏览器 JSON.parse 会炸）。"""
    value = float(value)
    if not np.isfinite(value):
        return None
    return round(value, ndigits)
