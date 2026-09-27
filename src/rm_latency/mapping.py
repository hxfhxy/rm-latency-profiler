"""字段映射：把任意队伍的遥测字段路径适配到本工具的规范字段名。

设计决策（D1/D7）：通道按 schema 指纹自动发现；字段路径默认内置 ACE 约定，
格式不同的队伍提供一份 YAML 覆盖即可，不需要改工具代码。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# 规范字段 -> 默认（ACE 遥测）JSON 路径。
# canonical 列名被 ingest/analysis/report 三层依赖，改名 = 全链路破坏性变更。
DEFAULT_FIELD_PATHS: dict[str, str] = {
    "seq": "header.seq",
    "stamp_ns": "header.stamp_ns",
    "recv_ns": "header.recv_ns",
    "capture_ns": "timing.capture_ns",
    "submit_ns": "timing.submit_ns",
    "finish_ns": "timing.finish_ns",
    "queued": "serial_tx.queued",
    "queue_drops": "serial_tx.queue_drops",
    "popped": "serial_tx.popped",
    "encode_rejected": "serial_tx.encode_rejected",
    "written": "serial_tx.written",
    "bytes_written": "serial_tx.bytes_written",
    "write_failures": "serial_tx.write_failures",
}

DEFAULT_FINGERPRINT = "capture_to_submit_ms"


class MappingError(ValueError):
    """映射文件格式非法。"""


@dataclass(frozen=True)
class TelemetryMapping:
    """一次摄取所用的完整映射：指纹 + 字段路径。"""

    fingerprint: str = DEFAULT_FINGERPRINT
    field_paths: dict[str, str] | None = None

    @property
    def paths(self) -> dict[str, str]:
        return self.field_paths or DEFAULT_FIELD_PATHS


DEFAULT_MAPPING = TelemetryMapping()


def load_mapping(path: str | Path) -> TelemetryMapping:
    """从 YAML/JSON 文件加载映射；未提供的键回落到默认值。

    文件结构：
        fingerprint: "某队遥测 schema 里的指纹字符串"
        fields:
          stamp_ns: header.stamp_ns     # 规范名: 消息内的点分路径
          capture_ns: timing.capture_ns
          ...
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise MappingError("读取 YAML 映射需要 pyyaml：pip install pyyaml") from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)

    if not isinstance(data, dict):
        raise MappingError(f"映射文件顶层必须是键值对象：{path}")

    fingerprint = data.get("fingerprint", DEFAULT_FINGERPRINT)
    if not isinstance(fingerprint, str) or not fingerprint:
        raise MappingError("fingerprint 必须是非空字符串")

    unknown = {k for k in (data.get("fields") or {}) if k not in DEFAULT_FIELD_PATHS}
    if unknown:
        raise MappingError(
            f"未知规范字段名：{sorted(unknown)}；合法名字见 README 字段表"
        )

    fields = dict(DEFAULT_FIELD_PATHS)
    for key, value in (data.get("fields") or {}).items():
        if not isinstance(value, str) or not value:
            raise MappingError(f"字段 {key} 的路径必须是非空字符串")
        fields[key] = value
    return TelemetryMapping(fingerprint=fingerprint, field_paths=fields)


def resolve(message: dict, dotted_path: str):
    """按点分路径取嵌套 JSON 值；任何一层缺失返回 None。"""
    node = message
    for part in dotted_path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node
