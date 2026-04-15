# DingTalk 入站链路重构专项方案

Date: 2026-04-14

## 背景

在完成 `DingTalk` 适配器第一轮能力增强后，用户在真实环境中发送了一个 `PDF` 文件用于验收。此前的现象是：

- 用户在钉钉中发送文件后，Hermes 没有给出任何反馈
- `gateway.run` 中没有出现对应的 `inbound message`
- 早期判断一度偏向“平台能力限制”或“企业内部机器人不支持文件回调”

为避免继续凭猜测推进，本次排查加入了最底层的 raw callback 日志，对真实链路进行了实证分析。结论是：

- `DingTalk` 已经把文件事件投递到 Hermes
- raw callback 中明确包含 `msgtype=file`
- payload 中明确包含 `fileName` 和 `downloadCode`
- 问题不在传输层，而在 `DingTalk` 适配器内部的入站归一化与事件构建链路

因此，本方案不再把问题定义为“补一个 PDF 分支”或“补一个文件 if 判断”，而是定义为：对 `DingTalk` 入站消息链路进行一次面向结构稳定性的重构。

## 真实故障证据

本次调试已经拿到如下关键证据：

1. 网关已经成功重启并重新连上 `DingTalk Stream`
2. raw callback 已经被记录到日志
3. raw callback 中的真实消息结构如下：

```text
msgtype = "file"
content.fileName = "claude-code-haha-report(1).pdf"
content.downloadCode = "..."
```

4. 但在这条 raw callback 之后，没有继续出现标准化后的 `inbound message`

这说明：

- 文件消息已经到达 Hermes 的 `DingTalk` 入口
- 但没有成功穿透到统一的 `MessageEvent` 层
- 断点位于 `raw callback -> SDK 对象 -> DingTalkAdapter._on_message() -> MessageEvent`

## 当前链路现状

基于 `gateway/platforms/dingtalk.py` 当前实现，文件消息的大致流程如下：

1. `dingtalk-stream` websocket 收到回调
2. `_IncomingHandler.process()` 取出 `callback_message.data`
3. raw payload 被记录到日志
4. raw payload 被交给 `dingtalk_stream.ChatbotMessage.from_dict(...)`
5. `DingTalkAdapter._on_message()` 依赖 SDK 对象中的 `message_type`、`content`、`downloadCode`
6. 若识别为文件，则继续下载附件、缓存文件并组装 `MessageEvent`
7. 再调用 `handle_message(event)` 进入 Hermes 主链路

本次故障说明，这条链路当前至少存在一个结构性风险：

- raw payload 中已经有文件语义
- 但如果 SDK 对象没有把这些字段按 Hermes 预期暴露出来
- 后续 `_on_message()` 就无法正确识别该消息
- 最终导致“文件明明收到了，但内部看起来像没收到”

## 问题根因

本次问题的根因不是 DingTalk 没投递文件回调，也不是附件下载接口本身不可用，而是：

1. `DingTalk` 适配器在入站归一化阶段过度依赖第三方 SDK 的对象化结果
2. `raw payload` 没有被当作文件消息识别的一级真相源
3. 消息类型判断、附件提取、附件下载、事件组装过度耦合在 `_on_message()` 中
4. 对 `file/document` 这类结构化消息缺少稳定的 raw-first 归一化策略

从架构角度看，当前问题不是单个字段 bug，而是“平台消息归一化层边界不清晰”的结果。

## 对其他适配器的借鉴结论

本次对比了仓库中更成熟、且与企业 IM 附件场景更接近的适配器，重点参考了 `Feishu` 和 `WeCom`。

### Feishu 的借鉴价值

`Feishu` 的优点不在于某个具体附件函数，而在于它把“归一化”和“分发”拆得更清楚：

- 入站事件先做标准化
- `raw_message` 保留原始平台数据
- 再进入批处理、ACK、分发等后续流程

这说明更合理的设计应该先形成 Hermes 自己的稳定中间表示，而不是让平台 SDK 对象直接决定下游流程。

### WeCom 的借鉴价值

`WeCom` 的优点在于它对图片和文件的处理更接近 raw-first 模式：

- 直接从原始 payload 读取 `msgtype`
- 直接从原始 payload 提取媒体引用
- 下载后根据真实 MIME 推导最终消息类型
- 最终统一组装 `MessageEvent`

这条思路尤其适合 `DingTalk` 的 `file/picture/richText` 消息，因为结构化媒体消息最容易受到 SDK 映射差异的影响。

### 结论

`DingTalk` 后续重构应当：

- 在“分层边界”上向 `Feishu` 对齐
- 在“入站媒体处理哲学”上向 `WeCom` 对齐

也就是：

- raw payload 是一级语义来源
- SDK 对象只是补充来源
- 归一化与分发分层
- 下载结果与真实 MIME 参与最终消息类型判定

## 重构目标

本次专项重构的目标如下：

1. 让 `DingTalk` 文件消息在 raw callback 收到后，能够稳定进入 Hermes 的统一 `MessageEvent` 层
2. 让 `PDF`、文档、文本文件、Markdown 文件等通用附件与图片走不同但统一管理的下载路径
3. 让消息类型推断不再只依赖 SDK 对象，而由 raw payload 和真实媒体结果共同决定
4. 让 `DingTalk` 入站链路更易观测、更易调试、更易扩展
5. 为未来支持音频、视频、富文本混合消息预留合理扩展点

## 设计原则

1. `raw payload` 是平台语义的一级真相源
2. 第三方 SDK 对象只能作为辅助输入，不可作为唯一输入
3. 传输、归一化、附件解析、事件组装、分发必须明确分层
4. 文件消息不能因为没有文本正文而被静默丢弃
5. 图片和文件必须走不同的验证与缓存逻辑
6. `raw_message` 应保留原始平台 payload，便于调试和追溯
7. 所有关键阶段都要有可观测日志，但日志内容需适当截断，防止刷爆

## 推荐架构

建议将 `DingTalk` 入站处理重构为五层结构。

### 1. transport layer

职责：

- 维护 `dingtalk-stream` 连接
- 接收 `callback_message`
- 记录 raw callback 预览
- 执行 ACK 返回

这一层不负责：

- 识别消息类型
- 提取下载码
- 下载附件
- 构造 `MessageEvent`

### 2. normalization layer

职责：

- 直接基于 raw payload 提取稳定语义字段
- 形成 Hermes 自己的 DingTalk 中间表示

建议引入中间结构，例如：

```python
@dataclass
class DingTalkInboundEnvelope:
    raw_payload: dict
    raw_msg_type: str
    message_id: str
    conversation_id: str
    conversation_type: str
    sender_id: str
    sender_staff_id: str | None
    sender_nick: str | None
    session_webhook: str | None
    create_at_ms: int | None
    text: str
    image_refs: list[dict]
    file_refs: list[dict]
```

其中：

- `raw_msg_type` 优先来自 raw payload 的 `msgtype`
- `text` 优先从 raw payload 提取，必要时再用 SDK 补充
- `image_refs`、`file_refs` 优先从 raw payload 中的媒体结构提取

### 3. attachment resolution layer

职责：

- 根据 `downloadCode` 执行下载
- 严格区分图片与通用文件
- 返回 Hermes 可消费的本地资源结果

建议引入结构，例如：

```python
@dataclass
class ResolvedAttachment:
    kind: Literal["image", "file"]
    local_path: str
    filename: str | None
    mime_type: str | None
    size_bytes: int | None
    download_code: str | None
    raw_message_type: str | None
```

建议职责拆分如下：

- `_download_message_file(download_code)`：只负责下载远端内容
- `_resolve_image_attachment(...)`：只负责图片验证与图片缓存
- `_resolve_file_attachment(...)`：只负责文件落盘与文件 MIME 推断

要求：

- 非图片绝不能进入 `cache_image_from_bytes`
- PDF、文本、Markdown、Office 文件等统一走文件缓存路径
- 文件名优先使用平台提供值，其次再推导扩展名

### 4. event assembly layer

职责：

- 把文本、图片、文件整合成统一 `MessageEvent`

建议规则如下：

- 只有文本 -> `MessageType.TEXT`
- 只有图片 -> `MessageType.PHOTO`
- 文本 + 图片 -> `MessageType.TEXT`，同时附带 `media_urls`
- 文件消息 -> `MessageType.DOCUMENT`
- 文件消息即使没有文本，也不能被跳过

`raw_message` 字段建议保存 raw payload，而不是 SDK 对象。

### 5. dispatch layer

职责：

- 将构造完成的 `MessageEvent` 交给 `handle_message(event)`
- 如需增加去重、媒体聚合、ACK、批处理、日志采样，在这一层做平台特有处理

## 消息类型映射建议

建议在 `DingTalk` 中采用如下映射：

- `text` -> `MessageType.TEXT`
- `picture` -> `MessageType.PHOTO`
- `richText` 且只有文本 -> `MessageType.TEXT`
- `richText` 且文本 + 图片 -> `MessageType.TEXT`
- `richText` 且只有图片 -> `MessageType.PHOTO`
- `file` / `document` -> `MessageType.DOCUMENT`

最终类型判断建议遵循以下优先级：

1. 先看 raw payload 的声明类型
2. 再看是否成功解析出图片或文件引用
3. 下载完成后，用真实 MIME 做最终兜底

例如：

- raw payload 说是 `file`，且下载后是 `application/pdf` -> `DOCUMENT`
- raw payload 说是 `picture`，但真实内容不是图片 -> 记录异常，不走图片缓存

## 推荐职责拆分

建议将当前 `DingTalkAdapter._on_message()` 中的职责拆出为以下辅助函数：

1. `_build_inbound_envelope(raw_data, sdk_message=None)`
2. `_extract_text_from_payload(raw_data, sdk_message=None)`
3. `_collect_image_refs(raw_data, sdk_message=None)`
4. `_collect_file_refs(raw_data, sdk_message=None)`
5. `_resolve_attachments(envelope)`
6. `_derive_message_type(envelope, resolved_attachments)`
7. `_build_message_event(envelope, resolved_attachments)`
8. `_dispatch_inbound_event(event)`

这样做的好处是：

- 每层职责单一，易于测试
- 出问题时更容易定位
- 未来增加更多消息类型时，只需要扩展归一化层和附件层

## 日志与可观测性

本次重构必须把可观测性作为正式要求，而不是临时调试手段。

建议保留以下关键日志：

1. `raw callback received`
   - 记录截断后的 raw payload
   - 带 `msg_id`、`msgtype`

2. `payload normalized`
   - 记录 envelope 结果
   - 带 `raw_msg_type`、`text_len`、`image_ref_count`、`file_ref_count`

3. `attachments resolved`
   - 记录下载与缓存结果
   - 带 `resolved_count`、`mime_types`、`filenames`

4. `event dispatched`
   - 记录最终分发结果
   - 带 `message_type`、`media_count`、`attachment_count`

5. `attachment resolution failed`
   - 记录失败原因
   - 带 `download_code`、`category`、`raw_msg_type`

日志要求：

- 保证可检索
- 不刷爆日志
- 不直接输出大文件内容
- 允许快速判断消息卡在哪一层

## 迁移策略

本次重构建议采用“平移式重构”，而不是在当前 `_on_message()` 上继续叠加条件分支。

推荐顺序：

1. 先新增 raw-first 归一化函数
2. 再把现有图片/文件逻辑逐步迁入新分层
3. 保留旧函数短期兼容，避免一次性大改引入回归
4. 当新链路通过测试后，再清理旧路径

这样可以降低以下风险：

- 文本消息回归
- 图片消息回归
- session webhook 路由被误伤
- 下载逻辑与事件逻辑相互污染

## 实施任务建议

后续实施建议拆分为以下任务：

### 任务 1：建立 raw-first 入站 envelope

- 新增 `DingTalkInboundEnvelope`
- 让 transport 层先构造 envelope，而不是只依赖 SDK 对象
- 保留 SDK 作为补充输入

### 任务 2：重构原始 payload 归一化

- 抽离文本提取、图片引用提取、文件引用提取
- 让 `downloadCode`、`fileName` 优先从 raw payload 获取
- 让 `message_type` 优先从 raw payload 获取

### 任务 3：重构附件解析管线

- 统一下载入口
- 分离图片缓存与文件缓存
- 用真实 MIME 兜底消息类型

### 任务 4：重构 `MessageEvent` 组装

- 让 `raw_message` 存 raw payload
- 明确 `TEXT` / `PHOTO` / `DOCUMENT` 判定规则
- 文件消息无文本时也能进入主链路

### 任务 5：补日志与诊断点

- 增加结构化调试日志
- 明确每层的成功与失败输出

### 任务 6：补自动化测试

至少覆盖：

- raw payload 的 `file` 回调归一化
- SDK 丢字段时的 raw fallback
- 文件消息无文本仍生成 `MessageType.DOCUMENT`
- 图片与文件不会走错缓存路径
- `downloadUrl` 二跳下载
- 附件下载失败的日志与降级行为

## 验收标准

重构完成后，至少应满足以下验收结果：

1. 发送普通文本，行为与当前一致
2. 发送图片，行为与当前一致或更稳定
3. 发送 `PDF` 时，Hermes 能稳定生成文件类型入站事件
4. 文件消息即使没有正文文本，也不会被静默丢弃
5. 日志中可以清楚判断消息停留在哪一层
6. 后续扩展 `audio/video` 时，不需要再改 transport 层

## 非目标

本专项重构不包含：

- PDF 内容理解
- 文档解析、OCR、知识库入库
- MinerU 或外部文档理解工具的集成
- 用全新自研传输层替换 `dingtalk-stream`

这些能力都应建立在“文件已经稳定进入 Hermes 统一事件层”的前提之上。

## 总结

本次 `PDF` 验收失败已经证明，`DingTalk` 当前问题不是“没收到文件”，而是“收到了文件，但没有完成内部归一化”。

因此，后续正确方向不是继续为 `file` 消息补更多分支，而是将 `DingTalk` 入站链路重构为：

- transport 稳定接收
- normalization raw-first
- attachment resolution 职责清晰
- event assembly 稳定统一
- dispatch 易观察、易扩展

这次重构完成后，`DingTalk` 才能真正达到与 Hermes 其他成熟 channel 一致的入站能力水平，并为后续图片、文件、音频、视频等更多消息类型扩展打下正确基础。
