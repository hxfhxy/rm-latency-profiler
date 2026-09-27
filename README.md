# rm-latency-profiler

RoboMaster 视觉链路**黑盒延迟分析工具**：不吃队内代码一行，直接消费调试录像，
回答三个问题——**延迟有多大、瓶颈在哪一段、异常发生在什么时候**。

## 它做什么

- **MCAP 离线分析**：读 Foxglove 调试录像里的逐帧遥测，输出三段延迟
  （capture→submit 传输段 / submit→finish 计算段 / 端到端）的
  p50/p95/p99 分位数、全程时序图、分布直方图、帧率与掉帧检测、
  串口下行增量时间线，汇总为一份单文件 HTML 报告（离线可打开）。
- **视频物理测延迟**（开发中）：输入慢动作视频（LED 靶板 + 云台响应），
  自动检测状态跳变帧，测量**含下位机执行段**的真实物理端到端延迟——
  这是软件打点结构上测不到的部分。

## 快速开始

```bash
pip install -e .

# 交互式 Web 界面（推荐）：拖入录像，缩放即重算，多录像叠加对比
rm-latency serve          # 浏览器自动打开 http://127.0.0.1:8321

# 看一眼录像概况（--json 可机器读取）
rm-latency inspect run.mcap

# 生成静态 HTML 报告（支持一次多份录像），适合存档分享
rm-latency report run.mcap -o report/
# -> report/run-latency-report.html

# 导出逐帧数据 CSV，便于自行分析
rm-latency export run.mcap -o frames.csv
```

一份合成录像生成的[示例报告](docs/example-report.html)可以直接用浏览器打开看效果。

## 接入别的队伍的遥测格式

通道按 schema 指纹自动发现，字段路径可经映射文件适配——不改工具代码：

```yaml
# my-team.yaml
fingerprint: "snap"          # 本队 jsonschema 文本中必然出现的字符串
fields:                      # 规范名: 消息内点分路径（未列出的回落默认值）
  stamp_ns: snap.t_cam
  capture_ns: snap.t_cam
  submit_ns: snap.t_in
  finish_ns: snap.t_out
```

```bash
rm-latency report run.mcap -m my-team.yaml
```

规范字段名全集见 `src/rm_latency/mapping.py` 的 `DEFAULT_FIELD_PATHS`；
分析至少需要 `stamp_ns / capture_ns / submit_ns / finish_ns` 四个字段。

## 遥测格式约定（默认 ACE 格式）

工具按 schema 内容自动发现遥测通道：任何 jsonschema 编码的通道，只要包含
`capture_to_submit_ms` 字段即被识别。逐帧消息需要：

| 字段 | 说明 |
|------|------|
| `header.stamp_ns` / `recv_ns` | 单调时钟域时间戳（字符串形式的 int） |
| `timing.capture_ns / submit_ns / finish_ns` | 三段计时锚点 |
| `timing.capture_to_submit_ms` | 通道识别指纹 |
| `serial_tx.*` | 累计下行计数（可选，全 0 时报告会注明） |

设计决策与理由见 [docs/decisions.md](docs/decisions.md)。

## 开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest          # 全部基于合成录像，不需要真实数据
ruff check .
```

CI 在 push/PR 时跑 ruff + pytest（Python 3.10 / 3.12 矩阵）。

## Roadmap

- [x] MCAP 摄取 + 逐帧遥测表
- [x] 三段延迟分位数 + HTML 报告
- [x] 帧率/抖动/掉帧检测 + 串口下行时间线
- [x] 跨队字段映射（YAML）
- [x] 交互式 Web 界面（视野缩放重算、多录像对比）
- [ ] 视频物理测延迟：慢动作视频测含下位机的真实端到端延迟
- [ ] 发 PyPI / GitHub Release

## License

MIT
