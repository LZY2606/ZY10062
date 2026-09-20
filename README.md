# framebench — 离线二进制会话复盘工作台

不连接真实设备，复盘一段带时间戳和方向的二进制会话：按会话键重组分片、
校验 CRC、驱动握手状态机，并把每个输出事件关联回原始字节范围。

## 快速开始

```bash
python3 -m pip install -e '.[test]'
pytest -q && python3 -m framebench --host 127.0.0.1 --port 5200
# 浏览器打开 http://127.0.0.1:5200
```

页面上新建捕获 → 点「填入示例」（数据来自后端生成器，页面不内嵌样例）
→ 上传帧。也可以用 CLI 生成示例帧 JSON：

```bash
python3 -m framebench gen-example > frames.json
```

## 分析语义

- **可恢复告警（warning）**：重复帧、乱序（窗口内缓存重排）、序号缺口、
  校验和失败、时间回拨、半个 UTF-8 字段、重新同步。会话进入「恢复中」。
- **终止错误（fatal）**：未知消息类型、当前状态下不允许的消息。会话被
  「拒绝」，后续流量仅记录为已忽略。
- **绝不补字节**：缺口只报告缺失的序号区间，不合成任何不存在的字节；
  所有事件的字节区间都指向真实上传的帧。
- 事件编号 `e0000…` 是 (帧, 协议文档) 的纯函数，重放结果稳定。

会话状态在界面分四栏展示：待确认 / 已接受 / 被拒绝 / 恢复中。

## 分支与迁移

- 在任意事件上建分支；分支继承原捕获但从不修改原件。
- 变更算子：`mask_frame`（屏蔽帧）、`retime`（改变到达时间）、
  `replace_payload`（替换帧内字节区间）。
- 「与基线比较」给出两分支开始分歧的事件编号。
- 分支在创建时绑定协议版本；协议描述升级后旧分支仍绑定旧版本，
  只有显式「迁移协议版本」时才运行兼容检查（重放对比事件摘要），
  不兼容返回 409 与分歧报告，可勾选强制迁移。

## 导出与离线验证

「导出离线重放包」下载的 JSON 包含帧、协议文档、分支与事件摘要，
在另一台机器上无需服务即可验证：

```bash
python3 -m framebench replay framebench-cap-xxxxxxxxxxxx.json
```

退出码 0 表示重放的事件流与摘要逐字节一致。

## 持久化与崩溃恢复

数据目录（默认 `framebench-data/`，`--data` 可改）：

- `journal.log` — 追加式操作日志。每次状态变更先写入日志并 `fsync`，
  成功后才替换内存状态；写入失败时旧状态完全可读，接口返回 500。
- `snapshot.json` — 周期性快照，先写临时文件再原子 rename；
  上一份保留为 `snapshot.json.bak`。

**从一次被中断的写入恢复**：直接重启进程即可，无需手工干预。

1. 若中断发生在日志追加中途：最后一行是残缺 JSON，加载时在该处截断，
   之前的操作全部保留。
2. 若中断发生在快照写入中途：`snapshot.json` 可能损坏，加载器自动回退到
   `snapshot.json.bak`，再重放日志中较新的记录。
3. 若中断发生在「日志已写、内存未更新」之间：重启后日志重放会恢复该操作，
   状态不丢失。

## 幂等

所有 POST 接受 `Idempotency-Key` 头。同一键重复到达只返回第一次已确定的
结果（响应带 `"replayed": true`），不会重复应用变更；键与结果记入日志，
重启后仍然有效。网页在上传帧失败时保留同一幂等键，可直接安全重试。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/protocols` | 版本化协议描述列表 |
| GET | `/api/example` | 生成器示例帧（非页面内嵌） |
| POST | `/api/captures` | 新建捕获 `{name, protocol}` |
| POST | `/api/captures/{id}/frames` | 追加帧 `{frames:[…]}` |
| GET | `/api/captures/{id}` | 事件、会话状态、帧 |
| GET | `/api/captures/{id}/export` | 导出离线重放包 |
| POST | `/api/captures/{id}/branches` | 在锚点事件建分支 |
| POST | `/api/branches/{id}/mutations` | 追加变更算子 |
| GET | `/api/branches/{id}/diff` | 与基线的首个分歧事件 |
| POST | `/api/branches/{id}/migrate` | 协议迁移兼容检查 `{version, force}` |

帧格式：`{"id","session","ts","dir":"c2s"|"s2c","data":"<hex>"}`。

## 内置线格式（handshake@1 / handshake@2）

```
0      1        2      3      4-5     6-7       8..         end-2..end
magic  version  type   flags  seqBE   lenBE     payload     crc16BE
```

v1 与 v2 的 version 字节和 CRC 初值不同，用于演示协议升级后的兼容检查。
