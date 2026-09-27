"""MCAP 摄取：通道发现、遥测消息解析、帧表构建。

设计决策（详见 docs/decisions.md）：
- 遥测通道按 schema 内容识别（含 capture_to_submit_ms 即命中），
  不写死通道名/版本号，任何队的同形遥测都能接。
- 纳秒字段在遥测 JSON 里是字符串（规避 JSON 安全整数问题），读取时统一转 int64。
- serial_tx / debugger 计数是累计值，原样保留，差分在 analysis 层做。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from mcap.reader import make_reader

# 遥测 schema 的指纹字段：出现即认定该 channel 承载逐帧遥测
_TELEMETRY_FINGERPRINT = "capture_to_submit_ms"

# JSON 遥测里关心的字段 -> DataFrame 列名
_TIMING_INT_FIELDS = ("capture_ns", "submit_ns", "finish_ns")
_HEADER_INT_FIELDS = ("seq", "stamp_ns", "recv_ns")
_SERIAL_TX_FIELDS = (
    "queued",
    "queue_drops",
    "popped",
    "encode_rejected",
    "written",
    "bytes_written",
    "write_failures",
)
_DEBUGGER_FIELDS = ("attempted", "accepted", "consumed", "overwrites", "image_drops")


class TelemetryNotFoundError(RuntimeError):
    """文件里找不到符合指纹的遥测通道。"""


def find_telemetry_channel(path: str | Path) -> tuple[int, str]:
    """扫描 summary 里的 schema，返回 (channel_id, topic)。

    按 schema 文本内容识别而非通道名：团队改名、版本升级不影响识别。
    """
    with open(path, "rb") as f:
        summary = make_reader(f).get_summary()
        if summary is None:
            raise TelemetryNotFoundError(f"录像没有 summary（写入中断或非 MCAP）：{path}")
        for channel in summary.channels.values():
            schema = summary.schemas.get(channel.schema_id)
            if schema is None or schema.encoding != "jsonschema":
                continue
            text = schema.data.decode("utf-8", errors="replace")
            if _TELEMETRY_FINGERPRINT in text:
                return channel.id, channel.topic
    raise TelemetryNotFoundError(
        "未找到遥测通道：没有任何 jsonschema 通道包含指纹字段 "
        f"{_TELEMETRY_FINGERPRINT!r}；确认这是本工具支持的调试录像"
    )


def load_frames(path: str | Path) -> pd.DataFrame:
    """读取整份录像的遥测消息，构建逐帧 DataFrame。

    一行 = 一帧；列覆盖 header/timing/serial_tx/debugger 中与本工具相关的字段。
    时间列单位 ns，int64；缺失字段按 NaN 处理（容错部分旧版本录像）。
    """
    path = Path(path)
    _, topic = find_telemetry_channel(path)
    rows: list[dict] = []

    with open(path, "rb") as f:
        reader = make_reader(f)
        for _schema, _channel, message in reader.iter_messages(topics=[topic]):
            try:
                data = json.loads(message.data)
            except json.JSONDecodeError:
                continue  # 单条损坏不致命，跳过并在行数上可见

            row: dict = {"log_time_ns": message.log_time, "topic": topic}
            header = data.get("header") or {}
            timing = data.get("timing") or {}
            serial_tx = data.get("serial_tx") or {}
            debugger = data.get("debugger") or {}

            for key in _HEADER_INT_FIELDS:
                row[key] = _to_int(header.get(key))
            for key in _TIMING_INT_FIELDS:
                row[key] = _to_int(timing.get(key))
            for key in _SERIAL_TX_FIELDS:
                row[key] = _to_int(serial_tx.get(key))
            for key in _DEBUGGER_FIELDS:
                row[key] = _to_int(debugger.get(key))
            rows.append(row)

    if not rows:
        raise TelemetryNotFoundError(f"遥测通道存在但没有可解析的消息：{path}")

    df = pd.DataFrame(rows).sort_values("stamp_ns").reset_index(drop=True)
    return df


def _to_int(value) -> float:
    """纳秒/计数字段容错转换：字符串形式的 int -> int；缺失/非法 -> NaN。"""
    if value is None:
        return float("nan")
    try:
        return int(value)
    except (TypeError, ValueError):
        return float("nan")
