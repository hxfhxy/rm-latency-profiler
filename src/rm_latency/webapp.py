"""本地 Web 界面后端：FastAPI 分析服务。

设计决策（D9）：本地 Web 而非桌面 GUI——分析逻辑零复制复用 ingest/analysis，
浏览器是分析工具的行业形态，且规避无 sudo 环境的 Qt/打包问题。
安全边界：默认只绑定 127.0.0.1，这是单人本机工具，不是网络服务。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from . import analysis, ingest
from .mapping import MappingError, load_mapping

# 时序数组下发上限：超过则等步长抽稀（前端图表渲染上限，分析统计仍用全量）
_MAX_POINTS = 50_000

_STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="rm-latency-profiler", docs_url=None, redoc_url=None)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

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
        """上传一份 MCAP（可选映射文件），返回摘要 + 全量时序数据。

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
        finally:
            tmp_path.unlink(missing_ok=True)

        stats = analysis.frame_interval_stats(df)
        percentiles = analysis.percentile_table(df)
        gaps = _gap_indices(df)

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
                "gap_indices": gaps,
            }
        )

    return app


def _stride(n: int) -> int:
    import math

    return max(1, math.ceil(n / _MAX_POINTS))


def _gap_indices(df) -> list[int]:
    """疑似掉帧行号（interval 超判据），前端叠加标记。"""
    intervals = df["interval_ms"]
    median = float(intervals.median()) if len(intervals) else 0.0
    if median <= 0:
        return []
    return np.flatnonzero((intervals > max(analysis.GAP_FACTOR * median, 1.0)).to_numpy()).tolist()


def _timeline_payload(df) -> dict:
    stride = _stride(len(df))
    view = df.iloc[::stride]
    t = (view["stamp_ns"].to_numpy() - df["stamp_ns"].iloc[0]) / 1e9
    payload = {
        "t": [round(float(v), 4) for v in t],
        "gap_indices": [i // stride for i in _gap_indices(df)],
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
