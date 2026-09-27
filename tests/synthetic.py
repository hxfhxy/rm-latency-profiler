"""合成 MCAP 构造器：按 ACE 遥测格式写入 JSON schema 通道，供测试用。"""

from __future__ import annotations

import json
from pathlib import Path

from mcap.writer import Writer

_MIN_SCHEMA = b"""{
  "type": "object",
  "properties": {
    "header": {"type": "object"},
    "timing": {"type": "object", "properties": {
      "capture_ns": {"type": "string"},
      "submit_ns": {"type": "string"},
      "finish_ns": {"type": "string"},
      "capture_to_submit_ms": {"type": "number"},
      "submit_to_finish_ms": {"type": "number"}
    }},
    "serial_tx": {"type": "object"}
  }
}"""

# 32x32 白色 JPEG 夹具（ffmpeg 生成入库）：视频通道测试只需合法 JPEG 字节
_TINY_JPEG = (Path(__file__).parent / "fixtures" / "tiny.jpg").read_bytes()


def write_synthetic(
    path: Path,
    n_frames: int = 100,
    period_ms: float = 5.0,
    transport_ms: float = 1.0,
    compute_ms: float = 10.0,
    serial_written: bool = False,
    images: bool = False,
) -> None:
    """生成 n 帧已知延迟的合成遥测录像。

    第 i 帧的 capture_ns = i * period * 1e6（单调域），
    三段延迟 = 常数 + 每 25 帧一个 3 倍毛刺，用于验证分位数与掉帧检测。
    images=True 时附带同帧率的 foxglove.CompressedImage 通道（墙钟域 log_time），
    用于视频回放功能的测试。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        writer = Writer(f)
        writer.start()
        schema_id = writer.register_schema("ace.DebugFrame.v1", "jsonschema", _MIN_SCHEMA)
        channel_id = writer.register_channel("/ace/debug/auto_aim", "json", schema_id)
        image_channel = None
        if images:
            from foxglove_schemas_protobuf.CompressedImage_pb2 import CompressedImage

            img_schema_id = writer.register_schema(
                "foxglove.CompressedImage", "protobuf",
                b'{"type":"object"}',
            )
            image_channel = writer.register_channel(
                "/ace/debug/camera/image", "protobuf", img_schema_id,
            )

        written_cum = 0
        wall_base = 1_700_000_000_000_000_000
        for i in range(n_frames):
            capture = int(i * period_ms * 1e6)
            transport = transport_ms * (3 if i % 25 == 0 else 1)
            compute = compute_ms * (3 if i % 25 == 0 else 1)
            submit = capture + int(transport * 1e6)
            finish = submit + int(compute * 1e6)
            if serial_written:
                written_cum += 1
            msg = {
                "header": {"seq": i + 1, "stamp_ns": str(capture),
                           "recv_ns": str(capture + 100), "camera_index": 0},
                "timing": {
                    "capture_ns": str(capture), "submit_ns": str(submit),
                    "finish_ns": str(finish),
                    "capture_to_submit_ms": transport, "submit_to_finish_ms": compute,
                },
                "serial_tx": {"queued": written_cum, "queue_drops": 0, "popped": written_cum,
                              "encode_rejected": 0, "written": written_cum,
                              "bytes_written": written_cum * 32, "write_failures": 0},
                "debugger": {"attempted": 1, "accepted": 1, "consumed": 1, "overwrites": 0,
                             "image_drops": 0},
            }
            log_time = wall_base + capture  # 伪造墙钟域，与单调域偏移恒定
            writer.add_message(
                channel_id, log_time=log_time, publish_time=log_time,
                data=json.dumps(msg).encode(),
            )
            if image_channel is not None:
                img = CompressedImage(frame_id="camera_optical", format="jpeg")
                img.data = _TINY_JPEG
                writer.add_message(
                    image_channel, log_time=log_time, publish_time=log_time,
                    data=img.SerializeToString(),
                )
        writer.finish()
