# DingTalk 适配器实施计划

**目标：** 将 `Hermes` 的 `DingTalk` 适配器补齐为可用于生产的消息平台适配器，重点完成入站图片与文件/PDF 的端到端处理、出站 richer 消息能力、以及 SDK 稳定性加固。

**架构摘要：** 基于 `docs/plans/2026-04-14-dingtalk-adapter-architecture-design.md`，本计划按“先打通入站闭环，再扩展出站能力，最后做传输层加固”的顺序推进。优先让用户在钉钉里发图片和 PDF 时，Hermes 能正确接收、落盘并消费；随后再补 Hermes 回图、回文件的能力。

**技术栈：** `Python`、`dingtalk-stream`、`httpx`、Hermes gateway 事件模型、`pytest`

---

## 文件结构与职责

### 核心实现文件

- 修改：`gateway/platforms/dingtalk.py`
  - DingTalk 连接管理
  - 入站消息解析
  - `downloadCode` 提取
  - 图片/文件下载
  - 出站消息投递

- 修改：`gateway/platforms/base.py`
  - `MessageType`
  - `MessageEvent`
  - 可能新增附件字段或与文件相关的事件建模

### 测试文件

- 修改：`tests/gateway/test_dingtalk.py`
  - 适配器现有测试主入口
  - 扩展附件、错误恢复、出站 richer 消息测试

### 文档文件

- 已存在参考：`docs/plans/2026-04-14-dingtalk-adapter-architecture-design.md`
- 新增计划：`docs/plans/2026-04-14-dingtalk-adapter-implementation-plan.md`

---

## 实施顺序总览

1. 先补事件模型，给文件附件提供稳定承载。
2. 再补入站附件链路，彻底分离图片与文件。
3. 在入站闭环稳定后，补出站图片/文件能力。
4. 最后做 SDK 稳定性加固、日志、重试和补足测试。

---

## 任务 1：扩展事件模型，承载通用附件

**文件：**

- 修改：`gateway/platforms/base.py`
- 测试：`tests/gateway/test_dingtalk.py`

- [ ] **步骤 1：确认当前事件模型的限制**

当前 `MessageEvent` 只有：

```python
media_urls: List[str] = field(default_factory=list)
media_types: List[str] = field(default_factory=list)
```

它适合图片，但不适合承载 PDF 和通用文件。

- [ ] **步骤 2：为 `MessageEvent` 增加附件字段**

建议最小改动如下：

```python
attachments: List[Dict[str, Any]] = field(default_factory=list)
```

每个附件至少包含：

```python
{
    "kind": "image" | "file",
    "local_path": "...",
    "filename": "...",
    "mime_type": "...",
    "size_bytes": 123,
    "raw_message_type": "picture" | "file" | "document" | "richText",
}
```

保持 `media_urls` / `media_types` 不删，继续服务现有图片能力。

- [ ] **步骤 3：确认文件消息使用哪个现有枚举**

当前 `MessageType` 有：

```python
DOCUMENT = "document"
```

实施时优先复用 `MessageType.DOCUMENT`，避免新增 `FILE` 造成额外兼容面。

- [ ] **步骤 4：补测试，确认新字段不破坏旧行为**

新增最小测试目标：

- `MessageEvent` 默认 `attachments == []`
- 图片事件仍可只用 `media_urls`
- 后续 DingTalk 文件消息可以通过 `attachments` 暴露

- [ ] **步骤 5：运行针对性测试**

运行：

```bash
python -m pytest tests/gateway/test_dingtalk.py -q
```

预期：

- 旧测试不因 `MessageEvent` 结构变化而失败

---

## 任务 2：重构入站附件下载链路，分离图片与文件

**文件：**

- 修改：`gateway/platforms/dingtalk.py`
- 测试：`tests/gateway/test_dingtalk.py`

- [ ] **步骤 1：抽离通用下载入口**

从当前 `_download_image()` 中抽出通用逻辑，例如：

```python
async def _download_message_file(self, download_code: str) -> tuple[bytes, str, str | None]:
    ...
```

返回值建议包含：

- 原始字节
- `content_type`
- 远端建议文件名（如果能拿到）

保留以下已有逻辑：

- access token 获取
- `robot/messageFiles/download`
- `downloadUrl` 二次 GET
- timeout / error logging

- [ ] **步骤 2：保留并收敛图片专属路径**

图片逻辑改成只处理真实图片：

```python
async def _download_image(...):
    data, content_type, filename = await self._download_message_file(...)
    cached_path = cache_image_from_bytes(...)
```

要求：

- 若不是图片，直接失败返回
- 不再让非图片流入 `cache_image_from_bytes`

- [ ] **步骤 3：新增通用文件落盘路径**

新增例如：

```python
async def _download_attachment(...):
    data, content_type, filename = await self._download_message_file(...)
    # 写入 ~/.hermes/downloads/dingtalk/
```

要求：

- PDF、doc、txt、markdown 等都能正常保存
- 优先保留 DingTalk 提供的原始文件名
- 若无文件名，则根据 `content_type` 或 hash 推导

- [ ] **步骤 4：补文件下载的安全处理**

至少实现：

- 文件名清洗
- 路径穿越保护
- 目录固定到 `~/.hermes/downloads/dingtalk/`
- 可选大小上限钩子

- [ ] **步骤 5：补测试**

新增测试目标：

- `downloadUrl` JSON 响应解析
- 原始字节图片下载
- 原始字节 PDF 下载
- 非图片 payload 不进入图片缓存
- 文件名缺失时的扩展名推导

- [ ] **步骤 6：运行测试**

运行：

```bash
python -m pytest tests/gateway/test_dingtalk.py -q
```

预期：

- 新增下载与附件落盘测试通过

---

## 任务 3：补全 DingTalk 入站消息归一化

**文件：**

- 修改：`gateway/platforms/dingtalk.py`
- 修改：`gateway/platforms/base.py`
- 测试：`tests/gateway/test_dingtalk.py`

- [ ] **步骤 1：拆分图片和文件的 `downloadCode` 提取**

将当前 `_collect_download_codes()` 拆成更清晰的结构，例如：

```python
def _collect_image_download_codes(...)
def _collect_file_download_codes(...)
```

优先级：

- `picture`
- `richText` 内嵌图片
- `file` / `document` 通过 `raw_dict`

- [ ] **步骤 2：在 `_on_message()` 中分别处理图片和文件**

目标流程：

```python
image_codes = ...
file_codes = ...
images = await ...
files = await ...
```

归一化结果：

- 纯图片消息 -> `MessageType.PHOTO`
- 文本 + 图片 -> `MessageType.TEXT`
- 文件 / PDF -> `MessageType.DOCUMENT`

- [ ] **步骤 3：让文件消息不因“无文本”被丢弃**

当前逻辑：

```python
if not text and not media_urls:
    return
```

需要改成考虑 `attachments`，确保：

- 用户只发一个 PDF 时，消息不会被当作空消息跳过

- [ ] **步骤 4：将图片与文件都注入事件**

规则：

- 图片：继续填充 `media_urls` / `media_types`
- 文件：写入 `attachments`
- 混合消息：两者都保留

- [ ] **步骤 5：补测试**

至少覆盖：

- 入站 `picture`
- 入站 `richText` + 图片
- 入站纯 `file`
- 入站纯 `document`
- 无文本但有文件附件的消息不会被丢弃

- [ ] **步骤 6：运行测试**

运行：

```bash
python -m pytest tests/gateway/test_dingtalk.py -q
```

---

## 任务 4：整理出站层，为 richer 消息做结构准备

**文件：**

- 修改：`gateway/platforms/dingtalk.py`
- 测试：`tests/gateway/test_dingtalk.py`

- [ ] **步骤 1：把当前 `send()` 中的 markdown 发送逻辑抽为 helper**

例如：

```python
async def _send_markdown(...)
```

然后 `send()` 调它。

目的：

- 后续可以按消息类型分流，而不是把所有逻辑堆在 `send()`

- [ ] **步骤 2：为 richer 出站保留能力路由接口**

预留结构：

```python
async def _send_image(...)
async def _send_file(...)
```

即使 Phase 1 还未完全实现，也先把结构建好。

- [ ] **步骤 3：定义降级策略**

例如：

- 图片发送失败 -> 回 markdown 说明
- 文件发送失败 -> 回 markdown 说明
- 不支持 richer 类型 -> 自动回 markdown

- [ ] **步骤 4：补最小测试**

测试目标：

- `send()` 仍然能发 markdown
- richer 发送 helper 不会破坏现有发送逻辑

---

## 任务 5：实现出站图片与文件发送

**文件：**

- 修改：`gateway/platforms/dingtalk.py`
- 测试：`tests/gateway/test_dingtalk.py`

- [ ] **步骤 1：确认 DingTalk 当前机器人链路支持的 richer 出站方式**

这一任务的第一步不是盲写代码，而是核实：

- session webhook 是否支持 `image`
- session webhook 是否支持 `file`
- 是否需要先上传媒体再发送消息体引用

如果 webhook 不能直接发 richer 消息，则在计划中改为“上传 + 引用”路径。

- [ ] **步骤 2：实现图片发送**

目标接口：

```python
async def _send_image(self, chat_id: str, image_path: str, metadata: dict | None = None) -> SendResult:
    ...
```

要求：

- 从本地文件发图
- 明确失败原因
- 超时可重试

- [ ] **步骤 3：实现文件发送**

目标接口：

```python
async def _send_file(self, chat_id: str, file_path: str, metadata: dict | None = None) -> SendResult:
    ...
```

要求：

- 支持 PDF 和通用文件
- 文件不存在时给出明确错误
- 若 DingTalk 不支持此路径，执行可接受降级

- [ ] **步骤 4：将 richer 出站接入实际调用点**

规则建议：

- 默认 `send()` 仍收文本
- richer 回复通过 metadata 或上层显式调用 richer helper

- [ ] **步骤 5：补测试**

至少覆盖：

- 图片发送 payload / 上传链路
- 文件发送 payload / 上传链路
- richer 发送失败后的 markdown 降级

---

## 任务 6：加固 `dingtalk-stream` 连接稳定性

**文件：**

- 修改：`gateway/platforms/dingtalk.py`
- 测试：`tests/gateway/test_dingtalk.py`

- [ ] **步骤 1：整理异常分类**

把现有 transport 层错误分成：

- ping timeout
- SSL EOF
- 普通连接失败
- webhook timeout
- 下载 timeout

不要只打一类模糊日志。

- [ ] **步骤 2：统一重试与 backoff 策略**

对以下路径设明确策略：

- stream reconnect
- webhook send
- attachment download

例如：

```python
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
```

发送和下载也应有小规模重试，而不是一把梭。

- [ ] **步骤 3：把 noisy SDK 日志控制集中化**

当前已有：

```python
logging.getLogger("dingtalk_stream").setLevel(logging.CRITICAL)
```

需要决定：

- 默认是否保持压制
- 是否增加调试模式开关

- [ ] **步骤 4：确保单次附件失败不污染整个 stream loop**

要求：

- 单条消息下载失败只影响该消息
- 不导致 `_run_stream()` 退出

- [ ] **步骤 5：补测试**

至少覆盖：

- send timeout retry
- reconnect after exception
- attachment download exception 不导致整条消息处理崩坏

---

## 任务 7：补齐端到端测试与人工验收脚本

**文件：**

- 修改：`tests/gateway/test_dingtalk.py`
- 可选新增：`docs/plans/` 中附一段手工验收 checklist

- [ ] **步骤 1：整理测试矩阵**

自动化测试矩阵至少包括：

- 文本入站
- 图片入站
- richText 图文入站
- PDF / 文件入站
- markdown 出站
- 图片出站
- 文件出站
- timeout / retry
- reconnect

- [ ] **步骤 2：增加手工验收清单**

手工验收步骤建议固定为：

1. 钉钉发文本
2. 钉钉发图片
3. 钉钉发 richText 图文
4. 钉钉发 PDF
5. 钉钉发普通文件
6. Hermes 回 markdown
7. Hermes 回图片
8. Hermes 回文件

- [ ] **步骤 3：运行网关测试**

建议命令：

```bash
python -m pytest tests/gateway/test_dingtalk.py -q
```

如果需要跑更广范围：

```bash
python -m pytest tests/gateway -q
```

---

## 建议提交切分

建议至少按下面切分提交：

1. `feat: add DingTalk file-aware event model`
2. `feat: support inbound DingTalk file and PDF downloads`
3. `refactor: split DingTalk attachment handling by image and file`
4. `feat: add DingTalk outbound image and file delivery`
5. `fix: harden DingTalk stream reconnect and timeout handling`
6. `test: expand DingTalk adapter coverage`

---

## 阶段验收标准

### 第一阶段通过标准

- 钉钉发图可稳定接收
- 钉钉发 PDF / 文件可稳定落盘
- Hermes 可在事件里看到文件附件
- 文件不再走图片缓存逻辑

### 第二阶段通过标准

- Hermes 能在 DingTalk 发回图片
- Hermes 能在 DingTalk 发回文件
- richer 消息失败时有清晰降级

### 第三阶段通过标准

- 常见 timeout / EOF / reconnect 场景下不会频繁出现用户可见故障
- 自动化测试覆盖关键路径

---

## 实施备注

1. `DingTalk` 当前最大真实价值在于“文档与图片入站”，所以第一阶段优先级最高。
2. `MessageEvent` 的扩展必须尽量兼容现有平台，避免为了 DingTalk 破坏 Telegram / Feishu 等现有路径。
3. richer 出站能力在实现前必须先再次确认 DingTalk 当前 bot 路径的 API 能力，不应凭经验假设。
4. 若 Phase 2 发现 DingTalk 机器人链路对图片/文件出站存在不可绕过限制，应将设计调整为“能力检测 + 明确降级”，而不是强行补齐一个不稳定实现。
