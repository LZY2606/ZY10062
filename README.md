# framebench

离线二进制会话复盘工作台：不连接真实设备，上传带时间戳和方向的帧序列，
选择版本化协议描述，系统按会话键重组分片、验证校验和、驱动握手状态机，
并把每个输出事件关联回原始字节范围。

## 快速开始

```bash
python3 -m pip install -e '.[test]'
pytest -q
python3 -m framebench --host 127.0.0.1 --port 5200
# 浏览器打开 http://127.0.0.1:5200
```

生成一份示例捕获（含握手、分片、重复包、坏校验和、缺口、FIN 关闭）：

```bash
python3 -m framebench example > capture.jsonl
```

在页面粘贴该 JSONL（或点“载入示例”），选择协议版本后上传分析。

## 捕获格式

每行一条 JSON（JSONL）：

```json
{"ts": 1700000000000.0, "dir": "c2s", "data": "4e4201..."}
```

- `ts`：到达时间戳（毫秒，允许回拨，会产生 `time_rollback` 告警）
- `dir`：`c2s` 或 `s2c`
- `data`：帧原始字节的十六进制

## 分析规则

- **分片重组**：按 `(session, dir)` 会话键累积 `FRAG` 分片，直到非分片帧到达；
  捕获结束时仍悬置的分片记为 `fragment_abandoned` 错误，绝不补字节。
- **校验和**：CRC32 不匹配的帧被拒绝（`checksum_mismatch` 错误），不进入序号流。
- **缺口**：序号跳跃产生 `gap_detected` 错误，缺失字节绝不合成。
- **可恢复告警**：重复包、乱序、时间回拨、半个 UTF-8 字段、意外消息——
  记为 warning，帧进入“恢复中”列。
- **终止错误**：坏校验和、畸形帧、同序号不同内容、缺口——记为 error，
  仅拒绝受影响帧，不中止整体分析。
- **事件编号**：同一（捕获 + 协议版本 + 操作序列）重放必然得到相同的
  `evt-NNNN` 编号与摘要。

帧按状态分列展示：待确认（分片悬置）、已接受、被拒绝、恢复中。

## 分支

在任意事件处建立分支：屏蔽一帧（`mask_frame`）、改变到达时间（`retime`）、
替换 payload（`replace_payload`）。分支是叠加在原捕获上的有序操作列表，
**不改原件**；每次操作都写入日志可溯源。两个分支可通过
`GET /api/branches/{a}/diff/{b}` 比较出从哪个事件开始分歧。

协议描述升级后，旧分支仍绑定旧版本；只有显式调用
`POST /api/branches/{id}/migrate`（先检查、带 `confirm` 再执行）
才运行兼容检查并迁移。

## 导出与离线验证

`GET /api/branches/{id}/export` 下载 zip 包（捕获、协议描述、分支操作、
事件摘要、SHA-256 清单）。在另一台机器上离线重放并验证：

```bash
python3 -m framebench verify branch-<id>.zip
```

## 幂等

所有 POST 端点接受 `Idempotency-Key` 头。同一键重复到达时，
只返回第一次已经确定的结果，副作用不会重放。

## 从被中断的写入恢复

每次状态改变都按三步写入：

1. 向 `journal` 表提交一条 `pending` 意图；
2. 在独立事务中应用数据变更；
3. 把意图标记为 `done`。

进程在任意一步崩溃后：

```bash
python3 -m framebench recover --db framebench.db
# 或在页面/脚本里 POST /api/recover
```

恢复逻辑逐条核对 `pending` 意图：变更已落库的标记为 `done`，
未落库的标记为 `discarded`——无论哪种情况，**旧状态始终可读**，
日志行永不删除，完整可溯源。服务启动时会自动执行一次恢复。

## 测试

```bash
pytest -q
```

覆盖：重复/乱序/缺口/时间回拨/半个 UTF-8/坏校验和/畸形帧/分片悬置、
事件编号确定性、分支分歧点、协议迁移兼容检查、幂等重放、
中断写入恢复、导出包离线验证与篡改检测。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/protocols` | 可用协议版本 |
| GET | `/api/sample` | 示例捕获 JSONL |
| POST | `/api/captures` | 上传捕获（幂等） |
| GET | `/api/captures/{id}` | 捕获与原始记录 |
| POST | `/api/captures/{id}/branches` | 建分支（幂等） |
| GET | `/api/branches/{id}` | 分支 + 分析结果 |
| POST | `/api/branches/{id}/ops` | 追加覆盖操作（幂等） |
| GET | `/api/branches/{a}/diff/{b}` | 分歧点 |
| POST | `/api/branches/{id}/migrate` | 协议迁移兼容检查/确认 |
| GET | `/api/branches/{id}/export` | 导出离线重放包 |
| POST | `/api/recover` | 恢复中断的写入 |
| GET | `/api/journal` | 状态变更日志 |
