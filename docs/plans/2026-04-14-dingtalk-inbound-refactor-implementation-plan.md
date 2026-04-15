# DingTalk 入站链路重构实施计划

> **For agentic workers:** 建议按任务逐项执行，并在每个任务结束后做一次小范围回归验证。步骤使用 checkbox (`- [ ]`) 语法，便于跟踪。

**目标：** 将 `DingTalk` 文件/图片入站链路重构为 raw-first 架构，使文件消息能够稳定进入 Hermes 的统一 `MessageEvent` 层，并消除对 `dingtalk-stream` SDK 对象映射的脆弱依赖。

**架构摘要：** 基于 `docs/plans/2026-04-14-dingtalk-inbound-refactor-design.md`，本计划将现有 `DingTalk` 入站处理拆为 `transport`、`normalization`、`attachment resolution`、`event assembly`、`dispatch` 五层职责。实现策略采用平移式重构：先引入新的 raw-first 归一化与组装路径，再逐步迁移旧逻辑，最后清理冗余代码。

**技术栈：** `Python`、`dingtalk-stream`、`httpx`、Hermes gateway 事件模型、`pytest`

---

## 文件结构与职责

### 核心实现文件

- 修改：`gateway/platforms/dingtalk.py`
  - `DingTalk` transport 入口
  - raw payload 归一化
  - 图片/文件附件解析
  - `MessageEvent` 组装
  - 入站日志与分发

- 可能修改：`gateway/platforms/base.py`
  - 若需要补充 `MessageEvent` 字段说明或收敛附件字段使用约定

### 测试文件

- 修改：`tests/gateway/test_dingtalk.py`
  - raw-first 归一化测试
  - 文件消息回归测试
  - 图片/文件附件解析测试
  - 下载失败与日志路径测试

### 参考文档

- 设计文档：`docs/plans/2026-04-14-dingtalk-inbound-refactor-design.md`
- 既有总计划：`docs/plans/2026-04-14-dingtalk-adapter-implementation-plan.md`

---

## 实施顺序总览

1. 先把入站数据源从“SDK 优先”调整为“raw payload 优先”。
2. 再建立稳定的归一化中间结构，承载文本、图片引用、文件引用和会话元数据。
3. 然后拆分附件解析层，严格区分图片与通用文件。
4. 再统一事件组装与分发逻辑，确保文件消息无文本也能进入主链路。
5. 最后补日志、回归测试和清理旧路径。

---

## 任务 1：建立 raw-first 入站 envelope

**文件：**

- 修改：`gateway/platforms/dingtalk.py`
- 测试：`tests/gateway/test_dingtalk.py`

- [ ] **步骤 1：确认当前 transport 入口的输入与输出边界**

当前入口是：

```python
incoming = dingtalk_stream.ChatbotMessage.from_dict(raw_data)
await self._adapter._on_message(incoming)
```

问题是：

- raw payload 已经存在，但只被用于临时日志
- 后续主要消费的是 SDK 对象
- 一旦 SDK 字段映射不完整，文件消息就会丢失语义

- [ ] **步骤 2：引入 `DingTalkInboundEnvelope` 中间结构**

在 `gateway/platforms/dingtalk.py` 中新增一个轻量 dataclass，例如：

```python
@dataclass
class DingTalkInboundEnvelope:
    raw_payload: Dict[str, Any]
    raw_msg_type: str
    message_id: str
    conversation_id: str
    conversation_type: str
    sender_id: str
    sender_staff_id: Optional[str]
    sender_nick: Optional[str]
    session_webhook: Optional[str]
    create_at_ms: Optional[int]
    text: str
    image_refs: List[Dict[str, Any]]
    file_refs: List[Dict[str, Any]]
    sdk_message: Any = None
```

要求：

- `raw_payload` 为一级来源
- `sdk_message` 只作为补充，不作为必需字段

- [ ] **步骤 3：新增 `_build_inbound_envelope()`**

新增函数，例如：

```python
def _build_inbound_envelope(
    self,
    raw_payload: Dict[str, Any],
    sdk_message: Any | None = None,
) -> DingTalkInboundEnvelope:
    ...
```

该函数负责：

- 解析 `msgtype`
- 解析 `message_id`
- 解析 `conversation_id`
- 解析 `sender` 信息
- 解析 `session_webhook`
- 调用文本与附件引用提取函数

- [ ] **步骤 4：调整 `_IncomingHandler.process()` 调用链**

新路径建议为：

```python
raw_data = getattr(callback_message, "data", None) or {}
sdk_message = dingtalk_stream.ChatbotMessage.from_dict(raw_data)
envelope = self._adapter._build_inbound_envelope(raw_data, sdk_message=sdk_message)
await self._adapter._process_inbound_envelope(envelope)
```

要求：

- raw payload 日志保留
- transport 层不直接做附件判断
- `_on_message()` 可以先保留作兼容包装，但新逻辑应转向 envelope

- [ ] **步骤 5：补 envelope 构建测试**

新增测试目标：

- raw payload 为 `file` 时，`raw_msg_type == "file"`
- `message_id` 从 raw payload 正确提取
- `file_refs` 中能拿到 `downloadCode` 与 `fileName`
- SDK 对象缺字段时，envelope 仍能构建成功

- [ ] **步骤 6：运行针对性测试**

运行：

```bash
.venv/bin/python -m pytest -o addopts='' tests/gateway/test_dingtalk.py -q
```

预期：

- 新增 envelope 测试通过
- 现有文本/图片测试未回归

---

## 任务 2：拆分 raw payload 归一化职责

**文件：**

- 修改：`gateway/platforms/dingtalk.py`
- 测试：`tests/gateway/test_dingtalk.py`

- [ ] **步骤 1：拆出文本提取函数**

新增函数，例如：

```python
def _extract_text_from_payload(self, raw_payload: Dict[str, Any], sdk_message: Any | None = None) -> str:
    ...
```

提取优先级：

1. raw payload 中的显式文本字段
2. `richText` 的文本片段
3. SDK 对象中的 `text` / `rich_text_content`

- [ ] **步骤 2：拆出图片引用提取函数**

新增函数，例如：

```python
def _collect_image_refs(self, raw_payload: Dict[str, Any], sdk_message: Any | None = None) -> List[Dict[str, Any]]:
    ...
```

图片引用最少应包含：

```python
{
    "download_code": "...",
    "raw_message_type": "picture" | "richText",
}
```

- [ ] **步骤 3：拆出文件引用提取函数**

新增函数，例如：

```python
def _collect_file_refs(self, raw_payload: Dict[str, Any], sdk_message: Any | None = None) -> List[Dict[str, Any]]:
    ...
```

文件引用最少应包含：

```python
{
    "download_code": "...",
    "filename": "...",
    "raw_message_type": "file" | "document",
}
```

要求：

- `fileName` 优先从 raw payload 读取
- `downloadCode` 优先从 raw payload 读取
- 不依赖 SDK 恰好保留 `content`

- [ ] **步骤 4：清理旧的 `_collect_download_codes()` 责任**

处理方式二选一：

- 保留旧函数，但让它转调 `_collect_image_refs()` / `_collect_file_refs()`
- 或直接删掉旧函数，由 envelope 路径完全接管

推荐前者，先兼容过渡，再在最后清理。

- [ ] **步骤 5：补 raw-first 归一化测试**

新增测试目标：

- 真实 `file` payload 即使 `sdk_message.message_type` 不可靠，也能提取文件引用
- `picture` 和 `richText` 仍能提取图片引用
- `richText` 混合文本 + 图片时，文本和图片都能进入 envelope

- [ ] **步骤 6：运行测试**

运行：

```bash
.venv/bin/python -m pytest -o addopts='' tests/gateway/test_dingtalk.py -q
```

预期：

- 所有归一化测试通过
- 旧的图片能力保持可用

---

## 任务 3：重构附件解析层

**文件：**

- 修改：`gateway/platforms/dingtalk.py`
- 测试：`tests/gateway/test_dingtalk.py`

- [ ] **步骤 1：保留 `_download_message_file()` 作为统一下载入口**

该函数职责只保留：

- access token 获取
- `robot/messageFiles/download` 请求
- `downloadUrl` 二次 GET
- 返回 `(bytes, content_type, filename)`

要求：

- 不在这里判断 `PHOTO` / `DOCUMENT`
- 不在这里构造事件字段

- [ ] **步骤 2：将图片解析收敛到 `_resolve_image_attachment()`**

新增函数，例如：

```python
async def _resolve_image_attachment(self, ref: Dict[str, Any]) -> Optional[ResolvedAttachment]:
    ...
```

要求：

- 强校验真实内容为图片
- 只在通过验证后调用 `cache_image_from_bytes`
- 返回结构化附件结果

- [ ] **步骤 3：将文件解析收敛到 `_resolve_file_attachment()`**

新增函数，例如：

```python
async def _resolve_file_attachment(self, ref: Dict[str, Any]) -> Optional[ResolvedAttachment]:
    ...
```

要求：

- 保留平台提供的文件名
- 无文件名时根据 MIME 推导扩展名
- 调用 `cache_document_from_bytes`
- 返回结构化附件结果

- [ ] **步骤 4：补 `ResolvedAttachment` 结构**

在 `gateway/platforms/dingtalk.py` 中新增：

```python
@dataclass
class ResolvedAttachment:
    kind: Literal["image", "file"]
    local_path: str
    filename: Optional[str]
    mime_type: Optional[str]
    size_bytes: Optional[int]
    download_code: Optional[str]
    raw_message_type: Optional[str]
```

- [ ] **步骤 5：新增 `_resolve_attachments()` 聚合函数**

新增函数，例如：

```python
async def _resolve_attachments(
    self,
    envelope: DingTalkInboundEnvelope,
) -> tuple[list[ResolvedAttachment], list[str], list[str]]:
    ...
```

输出建议：

- `resolved_attachments`
- `media_urls`
- `media_types`

其中：

- 图片附件同时写入 `media_urls/media_types`
- 文件附件只写入 `attachments`

- [ ] **步骤 6：补附件解析测试**

新增测试目标：

- `file` payload 下载后生成 `ResolvedAttachment(kind="file")`
- `picture` payload 下载后生成 `ResolvedAttachment(kind="image")`
- 非图片字节不会进入图片缓存
- `application/pdf` 会走文件缓存
- `downloadUrl` 二跳路径仍然生效

- [ ] **步骤 7：运行测试**

运行：

```bash
.venv/bin/python -m pytest -o addopts='' tests/gateway/test_dingtalk.py -q
```

预期：

- 图片/文件解析测试通过
- 不引入旧图片流程回归

---

## 任务 4：重构事件组装与分发

**文件：**

- 修改：`gateway/platforms/dingtalk.py`
- 测试：`tests/gateway/test_dingtalk.py`

- [ ] **步骤 1：新增 `_derive_message_type()`**

新增函数，例如：

```python
def _derive_message_type(
    self,
    envelope: DingTalkInboundEnvelope,
    resolved_attachments: List[ResolvedAttachment],
) -> MessageType:
    ...
```

规则：

- 只有文件 -> `MessageType.DOCUMENT`
- 只有图片且无文本 -> `MessageType.PHOTO`
- 文本 + 图片 -> `MessageType.TEXT`
- 只有文本 -> `MessageType.TEXT`

- [ ] **步骤 2：新增 `_build_message_event()`**

新增函数，例如：

```python
def _build_message_event(
    self,
    envelope: DingTalkInboundEnvelope,
    resolved_attachments: List[ResolvedAttachment],
    media_urls: List[str],
    media_types: List[str],
) -> MessageEvent:
    ...
```

要求：

- `raw_message` 存 raw payload
- `attachments` 存结构化附件字典
- `media_urls` / `media_types` 只承载图片类资源

- [ ] **步骤 3：实现 `_process_inbound_envelope()`**

建议主流程为：

```python
async def _process_inbound_envelope(self, envelope: DingTalkInboundEnvelope) -> None:
    resolved_attachments, media_urls, media_types = await self._resolve_attachments(envelope)
    event = self._build_message_event(envelope, resolved_attachments, media_urls, media_types)
    await self._dispatch_inbound_event(event)
```

要求：

- 文件消息即使无文本也要继续
- 真正空消息才跳过

- [ ] **步骤 4：收敛 `_on_message()`**

可以保留 `_on_message()` 作为兼容包装，但其职责应简化为：

```python
async def _on_message(self, message: ChatbotMessage) -> None:
    raw_payload = ...
    envelope = self._build_inbound_envelope(raw_payload, sdk_message=message)
    await self._process_inbound_envelope(envelope)
```

如果无法安全获取 `raw_payload`，则明确标注该路径只为兼容测试保留。

- [ ] **步骤 5：补事件组装测试**

新增测试目标：

- `file` 消息无文本时生成 `MessageType.DOCUMENT`
- `picture` 消息无文本时生成 `MessageType.PHOTO`
- 文本 + 图片生成 `MessageType.TEXT`
- `raw_message` 保存 raw payload 而不是 SDK 对象

- [ ] **步骤 6：运行测试**

运行：

```bash
.venv/bin/python -m pytest -o addopts='' tests/gateway/test_dingtalk.py -q
```

预期：

- 文件消息终于进入 `MessageEvent`
- 旧的文本/图片路径继续通过

---

## 任务 5：补可观测性与诊断日志

**文件：**

- 修改：`gateway/platforms/dingtalk.py`
- 测试：`tests/gateway/test_dingtalk.py`

- [ ] **步骤 1：保留 raw callback 日志**

当前已经有 raw callback 预览日志，继续保留：

- 截断长度固定
- 避免输出超长 payload
- 带 `msgtype` 和 `msgId`

- [ ] **步骤 2：新增 envelope 归一化日志**

建议日志格式：

```python
logger.info(
    "[%s] Normalized inbound envelope: msg_id=%s type=%s text_len=%d image_refs=%d file_refs=%d",
    self.name,
    envelope.message_id,
    envelope.raw_msg_type,
    len(envelope.text),
    len(envelope.image_refs),
    len(envelope.file_refs),
)
```

- [ ] **步骤 3：新增附件解析日志**

建议记录：

- 成功下载的附件数量
- 文件名
- MIME
- 失败类别

- [ ] **步骤 4：新增最终分发日志**

建议记录：

- 最终 `MessageType`
- `media_urls` 数量
- `attachments` 数量

- [ ] **步骤 5：补日志行为测试**

新增测试目标：

- 文件消息经过新链路时会出现 envelope 级别日志
- 附件失败时会出现明确失败日志，而不是静默吞掉

- [ ] **步骤 6：运行测试**

运行：

```bash
.venv/bin/python -m pytest -o addopts='' tests/gateway/test_dingtalk.py -q
```

预期：

- 日志测试通过
- 不影响主流程行为

---

## 任务 6：清理旧路径并做回归验证

**文件：**

- 修改：`gateway/platforms/dingtalk.py`
- 修改：`tests/gateway/test_dingtalk.py`

- [ ] **步骤 1：清理被新链路替代的旧 helper**

待清理候选：

- 旧的 `_collect_download_codes()`，如果已完全被替代
- 旧的分散式消息类型判断分支
- 只为 SDK 对象路径服务、但已无实际价值的兼容逻辑

要求：

- 删除前先确认测试覆盖已足够
- 不要删除仍被其他测试依赖的兼容入口

- [ ] **步骤 2：整理 `dingtalk.py` 内部结构顺序**

建议顺序：

1. dataclass / 常量
2. transport helpers
3. normalization helpers
4. attachment resolution helpers
5. event assembly helpers
6. outbound helpers
7. stream handler

- [ ] **步骤 3：运行 `DingTalk` 全量测试**

运行：

```bash
.venv/bin/python -m pytest -o addopts='' tests/gateway/test_dingtalk.py -q
```

预期：

- `DingTalk` 相关测试全部通过

- [ ] **步骤 4：运行更小范围的网关回归测试**

运行：

```bash
.venv/bin/python -m pytest -o addopts='' tests/gateway -q
```

预期：

- 没有明显的跨平台回归

- [ ] **步骤 5：检查 lint / diagnostics**

运行或检查：

- `ReadLints` 查看 `gateway/platforms/dingtalk.py`
- 确认没有新增导入未使用、类型不匹配或明显语法问题

- [ ] **步骤 6：手动验收一次真实 PDF 链路**

建议验收步骤：

1. 重启网关
2. 从钉钉发送一个小型 `PDF`
3. 确认日志出现：
   - raw callback
   - normalized envelope
   - attachment resolved
   - event dispatched
4. 确认 Hermes 至少返回“已收到文件”或进入正常 agent 流程

---

## 验收标准

实施完成后，必须满足以下结果：

1. `DingTalk` raw callback 中的 `file` 消息能稳定构建为 `DingTalkInboundEnvelope`
2. `downloadCode` 与 `fileName` 即使 SDK 丢字段，也能从 raw payload 读取
3. `PDF` 与其他文档消息能进入 `MessageType.DOCUMENT`
4. 文件消息即使没有正文文本，也不会被丢弃
5. 图片与文件不会混用缓存逻辑
6. `raw_message` 保存原始 payload，便于后续排查
7. 日志足以判断消息停在哪一层

## 非目标

本计划不包含：

- PDF 内容解析
- OCR、知识库入库、MinerU 等文档理解工作流
- `DingTalk` 出站 richer 消息的新增重构
- 替换 `dingtalk-stream` 传输实现

这些内容应在入站链路稳定后单独推进。

## 风险提醒

1. `dingtalk-stream` SDK 未来升级后，字段映射可能继续变化
   - 通过 raw-first 架构可降低风险，但不能完全消除

2. `robot/messageFiles/download` 的返回形态可能随接口行为变化
   - 测试应覆盖 JSON `downloadUrl` 与原始字节两种路径

3. 旧测试可能依赖 SDK 路径的历史行为
   - 平移式重构有助于减少一次性破坏面

## 实施建议

执行时建议遵循以下节奏：

1. 每个任务结束后立刻跑 `tests/gateway/test_dingtalk.py`
2. 不要等所有任务完成后再集中调试
3. 优先保证 `file` 消息进入 `MessageEvent`
4. 在验收成功前，不要顺手扩 scope 到出站 richer 消息或文档理解
