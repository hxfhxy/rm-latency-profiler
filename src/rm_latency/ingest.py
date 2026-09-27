"""MCAP 摄取：通道发现、遥测消息解析、帧表构建。

设计决策（详见 docs/decisions.md）：
- 遥测通道按 schema 指纹内容识别（默认含 capture_to_submit_ms 即命中），
  不写死通道名/版本号，任何队的同形遥测都能接；字段路径可经映射文件适配。
- 纳秒字段在遥测 JSON 里是字符串（规避 JSON 安全整数问题），读取时统一转 int64。
- serial_tx 计数是累计值，原样保留，差分在 analysis 层做。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from mcap.reader import make_reader

from .mapping import DEFAULT_MAPPING, TelemetryMapping, resolve

# 需要转 int64 的纳秒字段（映射里的规范名）；其余字段同样走 _to_int 容错
_NS_FIELDS = frozenset({"seq", "stamp_ns", "recv_ns", "capture_ns", "submit_ns", "finish_ns"})


class TelemetryNotFoundError(RuntimeError):
    """文件里找不到符合指纹的遥测通道。"""


def find_telemetry_channel(
    path: str | Path, mapping: TelemetryMapping | None = None
) -> tuple[int, str]:
    """扫描 summary 里的 schema，返回 (channel_id, topic)。

    按 schema 文本内容识别而非通道名：团队改名、版本升级不影响识别。
    """
    mapping = mapping or DEFAULT_MAPPING
    with open(path, "rb") as f:
        try:
            summary = make_reader(f).get_summary()
        except Exception as exc:  # mcap 库对损坏/非 MCAP 输入抛多种异常，统一转译
            raise TelemetryNotFoundError(f"不是有效的 MCAP 文件或已损坏：{path}（{exc}）") from exc
        if summary is None:
            raise TelemetryNotFoundError(f"录像没有 summary（写入中断或非 MCAP）：{path}")
        for channel in summary.channels.values():
            schema = summary.schemas.get(channel.schema_id)
            if schema is None or schema.encoding != "jsonschema":
                continue
            text = schema.data.decode("utf-8", errors="replace")
            if mapping.fingerprint in text:
                return channel.id, channel.topic
    raise TelemetryNotFoundError(
        f"未找到遥测通道：没有任何 jsonschema 通道包含指纹字段 {mapping.fingerprint!r}；"
        "确认录像格式，或在映射文件里指定本队遥测的 fingerprint"
    )


def load_frames(path: str | Path, mapping: TelemetryMapping | None = None) -> pd.DataFrame:
    """读取整份录像的遥测消息，构建逐帧 DataFrame。

    一行 = 一帧，一列 = 一个规范字段；缺失字段按 NaN 处理（容错旧版录像）。
    """
    mapping = mapping or DEFAULT_MAPPING
    path = Path(path)
    _, topic = find_telemetry_channel(path, mapping)
    rows: list[dict] = []

    with open(path, "rb") as f:
        reader = make_reader(f)
        try:
            messages = list(reader.iter_messages(topics=[topic]))
        except Exception as exc:
            raise TelemetryNotFoundError(
                f"消息流读取失败（文件可能截断）：{path}（{exc}）"
            ) from exc
        for _schema, _channel, message in messages:
            try:
                data = json.loads(message.data)
            except json.JSONDecodeError:
                continue  # 单条损坏不致命，跳过并在行数上可见

            row: dict = {"log_time_ns": message.log_time, "topic": topic}
            for canonical, dotted in mapping.paths.items():
                row[canonical] = _to_int(resolve(data, dotted))
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
