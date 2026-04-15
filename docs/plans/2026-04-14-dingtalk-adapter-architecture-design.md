# DingTalk 适配器架构设计

Date: 2026-04-14

## 目标

Hermes 应该提供一个可用于生产环境的 `DingTalk` 适配器，能够支持稳定的实时消息处理、正确的入站附件处理，以及足够完整的出站能力，以满足日常 Agent 使用需求。

本设计聚焦于 `gateway/platforms/dingtalk.py` 及其周边的网关事件模型，使 DingTalk 在核心用户体验上达到 Hermes 其他消息平台已具备的水平。

目标结果包括：

- 稳定可靠的入站文本处理
- 稳定可靠的入站图片处理
- 真正端到端可用的入站文件处理，包括 PDF
- 超越“仅支持 markdown 回复”的更完整出站能力
- 对已知 `dingtalk-stream` SDK 不稳定性的韧性
- 对核心消息路径和失败路径的自动化覆盖

## 当前设计存在的问题

当前 DingTalk 适配器有五个结构性问题：

1. 入站图片处理比入站文件处理完整得多，导致适配器能力失衡。
2. 文件和文档消息虽然可能暴露 `downloadCode`，但后续处理链路仍默认它们是图片负载。
3. 出站消息实际上仍然只有 markdown，因此 Hermes 无法在 DingTalk 中完整参与附件工作流。
4. 适配器依赖官方 `dingtalk-stream` SDK，而该 SDK 在真实负载下已知会产生重连噪音、ping timeout 以及突发 EOF 等行为。
5. 当前实现缺少足够级别的自动化测试覆盖，无法让附件处理和传输行为的迭代保持高置信度。

## 设计原则

1. 将传输层关注点与业务层关注点分离。
2. 将图片与通用文件视为两种不同的附件类型。
3. 优先选择优雅降级，而不是直接丢消息。
4. 假设 DingTalk payload 结构会随 SDK 版本变化，必要时回退到原始 payload 解析。
5. 让附件处理具备可观测性，包括日志和结构化元数据。
6. 让适配器聚焦于归一化，而不是承担下游文档处理职责。
7. 按阶段推进设计，先确保入站文件正确性，再扩展更丰富的出站能力。

## 范围

本设计包括：

- DingTalk Stream 事件的入站消息归一化
- 图片和文件的附件提取与下载
- 将附件暴露给 Hermes 所需的事件建模
- 在 DingTalk 能力允许范围内支持文本、图片、文件的出站回复
- 连接稳定性、超时处理与重试行为
- 自动化测试与验收标准

本设计不包括：

- MinerU 集成或文档解析工作流
- OCR、PDF 理解或知识库入库逻辑
- 超出 DingTalk 当前需求之外的网关级通用附件抽象
- 用新的传输实现替换官方 `dingtalk-stream` SDK

## 高层架构

`DingTalk` 适配器应被视为四层结构：

1. `stream_transport`
   负责长连接 SDK、重连、token 刷新以及 session webhook 路由。
2. `payload_normalization`
   将 DingTalk 回调 payload 转换为稳定的内部表示，包括消息类型、文本、元数据和附件引用。
3. `attachment_resolution`
   将 `downloadCode` 解析为本地缓存的图片或文件，并明确区分图片负载与通用文件负载。
4. `outbound_delivery`
   将 Hermes 的标准化回复以当前最佳支持的消息模式发回 DingTalk，并在 richer 消息不可用时做降级。

## 当前适配器现状评估

基于当前 `gateway/platforms/dingtalk.py` 的实现：

- 入站文本已支持
- 入站图片已支持，路径包括 `picture` 和 `richText` 图片提取
- DingTalk 的 `messageFiles/download` 接口已接入
- 对返回 `downloadUrl` 的下载响应已做部分兼容处理
- 出站回复仍局限于通过 `session_webhook` 发送 markdown 文本

当前最大的缺口是：入站文件和文档消息还没有实现真正端到端的标准化处理。即使某些情况下已经能拿到 `downloadCode`，后续附件链路仍然偏向图片处理、图片缓存和图片 MIME 推断。

## 入站消息模型

适配器应将 DingTalk 消息类型归一化为清晰的内部契约。

建议映射如下：

- `text` -> `MessageType.TEXT`
- `picture` -> `MessageType.PHOTO`
- `richText` 且只有文本 -> `MessageType.TEXT`
- `richText` 且同时包含文本和图片 -> `MessageType.TEXT`，并附带图片附件
- `richText` 且只有图片 -> `MessageType.PHOTO`
- `file` / `document` -> `MessageType.FILE`
- 预留未来兼容类型：
  - `audio`
  - `video`

标准化后的入站事件应保留：

- message id
- source 元数据
- sender 元数据
- timestamp
- 原始 DingTalk message type
- 提取后的纯文本
- 附件元数据
- 下载后的本地附件路径

## 附件模型

适配器应停止将所有可下载负载都当作图片处理。

建议引入附件抽象，例如：

```python
@dataclass
class AttachmentRef:
    kind: str              # "image" or "file"
    local_path: str
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    download_code: str | None = None
    raw_message_type: str | None = None
```

`MessageEvent` 可以继续暴露 `media_urls` 和 `media_types` 以兼容现有图片能力，但通用文件也应通过一等附件字段暴露，而不是被强行塞进图片语义。

## 附件提取

附件提取阶段应明确拆分图片发现和文件发现。

建议职责拆分如下：

1. `extract_text_payload(message)`
2. `collect_image_download_codes(message)`
3. `collect_file_download_codes(message)`
4. `collect_attachment_metadata(message)`

提取逻辑应优先使用 SDK 提供的便利字段，在缺失时回退到原始 payload 检查，以保证对不同 SDK 版本和不同消息类的容错能力。

## 附件下载管线

当前 `_download_image()` 路径应被重构为一个通用下载管线。

建议形式如下：

```python
async def _download_message_file(download_code: str) -> DownloadedPayload:
    ...

async def _download_image(download_code: str) -> AttachmentRef | None:
    ...

async def _download_attachment(download_code: str, metadata: dict) -> AttachmentRef | None:
    ...
```

规则如下：

- 总是先获取 access token
- 调用 `robot/messageFiles/download`
- 若首个响应是包含 `downloadUrl` 的 JSON，则执行第二次 GET
- 若首个响应是原始字节，则直接使用
- 严格区分图片校验与通用文件落盘
- 绝不能将非图片负载送进仅适用于图片的缓存 helper

### 图片处理

对于图片：

- 校验负载确实是有效图片
- 通过图片缓存路径落盘
- 根据真实内容类型推断 `media_types`
- 将本地图片路径暴露给 Hermes，用于视觉工作流

### 文件处理

对于文件和文档：

- 落盘到独立下载目录，例如 `~/.hermes/downloads/dingtalk/`
- 若 DingTalk payload 提供真实文件名，则优先保留
- 若没有文件名，再根据内容类型推断扩展名
- 将其作为正常文件附件暴露，而不是媒体附件

这是让 PDF 工作流真正可用的关键改动。

## 事件构建

在标准化与下载完成后：

- 纯图片入站负载应继续填充 `media_urls`
- 文本 + 图片混合消息应保持 `TEXT`，同时附带图片附件
- `file/document` 消息应生成 `MessageType.FILE`
- 文件消息不能因为没有内联文本就被直接丢弃

若附件下载失败：

- 若有文本，仍然保留文本内容
- 记录足够详细的附件失败日志，便于调试
- 在可行时附带失败元数据
- 避免静默丢弃整条消息

## 出站投递

当前适配器只发送：

```json
{
  "msgtype": "markdown",
  "markdown": { ... }
}
```

后续应将其扩展为具备明确能力路由的 richer 出站层。

### 第一阶段出站

保留 markdown 作为默认回复形式，但将其封装为专用 helper：

- `send_markdown(...)`

### 第二阶段出站

新增对以下能力的支持：

- 当 markdown 不适合时，回退到 plain text
- 图片回复
- 文件回复

具体实现取决于当前 DingTalk 机器人 / session webhook 路径实际支持哪些消息类型。若 DingTalk 在 richer 消息发送前要求先上传媒体，则这条上传链路应封装在适配器内部，而不是暴露给调用方。

### 出站降级规则

当 richer 消息类型无法成功投递时：

- 回退到 markdown 文本
- 在附件投递失败时附带简洁说明
- 除非处于调试模式，否则不要把原始 API 错误直接暴露给最终用户

## 传输稳定性与 SDK 缓解策略

`dingtalk-stream` SDK 应被视为一个不完全可靠的传输依赖。

适配器应该围绕它做加固，而不是假设理想行为。

必须具备的韧性行为包括：

1. 默认抑制或降级已知 noisy 的 SDK 日志
2. 在适配器日志中区分连接失败、ping timeout 和 EOF 断连
3. 维护带上限的 reconnect backoff
4. 为发送与下载请求设置明确 timeout
5. 对瞬时出站失败进行带退避的重试
6. 确保突发附件下载失败不会污染整个 stream loop

可选的后续增强包括：

- 记录 reconnect 频率指标
- 记录附件下载失败率指标
- 提供带敏感信息脱敏的原始 DingTalk payload 调试模式

## 安全性考虑

适配器应保留当前 SSRF 和来源校验保护，并将其扩展到更丰富的附件处理路径。

必须具备的保护包括：

- 只信任匹配 DingTalk 官方域名的 session webhook
- 只跟随由 DingTalk 官方 API 流程返回的下载 URL
- 对附件大小设置上限，或对大文件进行安全流式处理
- 在本地落盘前清洗文件名
- 阻止本地写文件时的路径穿越
- 避免在日志中输出原始敏感 token 或完整私有 URL

## 测试策略

### 单元测试

增加以下聚焦测试：

- 从纯文本消息中提取文本
- 从 `picture` 消息中提取图片 `downloadCode`
- 从 `richText` 中提取图片
- 从原始 payload 中提取 `file/document` 的 `downloadCode`
- 解析 JSON 里的 `downloadUrl`
- 原始字节图片下载处理
- 通用文件下载处理
- 图片与文件的路由决策
- 空 payload 和 malformed payload 的行为

### 适配器集成测试

增加以下测试：

- 入站纯文本消息
- 入站图片消息
- 入站带图带文的 `richText`
- 入站文件 / PDF 消息
- 附件下载失败但文本仍保留
- 出站 markdown 回复
- timeout 场景下的出站重试
- stream 异常后的 reconnect 行为

### 手工验收测试

最终验收套件应包括真实 DingTalk 场景验证：

1. 从 DingTalk 发文本给 Hermes
2. 从 DingTalk 发单张图片给 Hermes
3. 从 DingTalk 发带内嵌图片的富文本消息
4. 从 DingTalk 发 PDF 给 Hermes
5. 从 DingTalk 发通用文件给 Hermes
6. Hermes 回 markdown 文本
7. Hermes 回一张图片
8. Hermes 回一个文件

## 推进计划

### 第一阶段：入站附件正确性

交付内容：

- 明确支持 `file/document` 提取
- 通用文件下载路径
- 图片 / 文件附件彻底分流
- 文件支持所需的事件模型更新
- 入站路径自动化测试

成功标准：

- 从 DingTalk 发来的 PDF 和通用文件能够本地保存，并以文件附件形式暴露给 Hermes
- 图片处理能力保持正常

### 第二阶段：出站能力扩展

交付内容：

- 按消息类型拆分的出站 helper
- 图片发送
- 文件发送
- 不支持场景下的优雅降级

成功标准：

- Hermes 能够在 DingTalk 中返回文本回复以及部分附件类型

### 第三阶段：传输层加固

交付内容：

- 更好的重试与重连行为
- 更干净的日志和更好的可观测性
- 更深的失败路径覆盖

成功标准：

- 在常见 SDK 断连和 timeout 场景下，适配器仍能保持可用

## 推荐实施顺序

1. 先修复入站文件标准化和下载处理。
2. 再补文件感知的事件建模。
3. 在扩展出站能力之前，先验证端到端 PDF / 文件摄入闭环。
4. 入站附件稳定后，再补出站图片 / 文件支持。
5. 最后收尾做传输层加固和测试覆盖扩展。

这个顺序优先保证最可能立即解锁真实使用场景的能力，尤其是文档和图片摄入能力。

## 验收标准

当以下条件全部满足时，可视为本轮 DingTalk 适配器建设完成：

- 入站文本消息稳定
- 入站图片消息稳定且可供 Hermes 使用
- 入站 PDF 和通用文件消息能够稳定下载并以文件形式暴露
- 文件消息不再走图片专属缓存逻辑
- Hermes 至少能发送一种超出 markdown 的 richer 出站消息，或者在 DingTalk 机器人约束下明确给出可接受的降级行为
- reconnect 和 timeout 行为不再频繁造成对操作可见的失败
- 主要附件路径和失败路径有自动化测试覆盖

## 开放问题

在实现阶段还需要进一步确认以下问题：

1. Hermes 当前使用的 DingTalk 机器人 / session webhook 路径，实际支持哪些 richer 出站消息类型？
2. 在不同 SDK 版本下，`file` 和 `document` 消息回调里哪些附件元数据字段是稳定存在的？
3. 当前网关级 `MessageEvent` 是否需要新增一等文件附件字段，还是可以通过兼容方式扩展？
4. 大文件下载的大小限制应该在适配器层统一做，还是延后交给下游工具策略？

这些问题不会阻塞本设计本身，但应在第二阶段实现前明确答案。
