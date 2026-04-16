# DingTalk 网关链路全面修复方案

Date: 2026-04-14

## 背景

在最近一轮 `DingTalk` 适配器增强与入站重构之后，真实环境已经暴露出不止一个单点问题，而是一组相互叠加的稳定性缺陷：

- 入站消息处理存在明确运行时错误
- 旧逻辑与新逻辑在同一个适配器文件内并存
- `Hermes` 给用户发送图片和文件的行为不稳定
- 某些“失败”会被 markdown fallback 掩盖，看起来像成功
- 当前测试覆盖了很多局部方法，但没有守住最关键的交界面

这说明当前问题不能再按“修一个报错”或“补一个图片分支”来处理，而应该被定义为：

**对 `gateway/platforms/dingtalk.py` 做一次结构收敛、发送链路修正与测试补强的全面修复。**

本方案的目标不是直接给出代码补丁，而是为后续实施提供一份完整、可执行、可分阶段推进的修复蓝图。范围严格限定在钉钉网关链路：

- 消息入站
- 图片入站
- 文件入站
- 文本/图片/文件出站

## 调研范围

本次调研覆盖了以下内容：

1. `gateway/platforms/dingtalk.py` 当前实现
2. `tests/gateway/test_dingtalk.py` 当前测试覆盖
3. `gateway/run.py` 与 `gateway/platforms/base.py` 中对图片/文件发送的调用路径
4. `docs/plans/2026-04-14-dingtalk-inbound-refactor-design.md` 现有设计意图
5. 近期钉钉相关提交与当前文件中的合并残留迹象

## 当前适配器现状

### 入站链路（现状）

当前钉钉入站消息的目标链路是：

1. `connect()`
2. `_IncomingHandler.process()`
3. `_build_inbound_envelope(raw_payload, sdk_message=...)`
4. `_resolve_attachments(...)`
5. `_build_message_event(...)`
6. `_process_inbound_envelope(...)`
7. `handle_message(event)`

这条链路的架构方向本身是合理的，即尝试把钉钉消息先归一化成一个稳定的 `DingTalkInboundEnvelope`，再构造 Hermes 统一事件。但当前实现并没有完全收敛到这一条路径上。

### 出站链路（现状）

当前图片/文件发送链路大致是：

1. `gateway/run.py` 或 `BasePlatformAdapter` 后处理阶段决定发送媒体
2. 针对图片调用 `send_image_file(...)`
3. `send_image_file(...)` 调用 `_upload_media_file(...)`
4. 上传成功后通过 `_send_payload(...)` 发送原生钉钉图片消息
5. 如果异常，则 fallback 到 markdown 文本消息

文件发送 `send_document(...)` 也是类似逻辑。

问题在于，这条出站链路依赖会话路由信息（核心是 `session_webhook`），而它当前并不是稳定显式透传，而是大量依赖内存缓存 `_session_webhooks` 的碰巧命中。

## 目标链路重排（本方案聚焦）

为避免范围漂移到 Agent 语义层，本方案统一按网关四条主链路组织：

1. **消息入站链路（文本）**
   - `callback_message.data` -> `DingTalkInboundEnvelope` -> `MessageEvent(TEXT)` -> `handle_message`
2. **图片入站链路**
   - raw payload 取 `downloadCode` -> 下载/缓存图片 -> `MessageEvent(PHOTO + media_urls)`
3. **文件入站链路**
   - raw payload 取 `downloadCode/fileName` -> 下载/缓存附件 -> `MessageEvent(DOCUMENT + attachments)`
4. **消息/图片/文件出站链路**
   - 网关决定发送类型 -> 钉钉适配器发送文本或原生媒体 -> 失败时降级策略与结果语义

上述四条链路统一依赖“会话路由信息”，不讨论大模型上下文管理。

## 已确认的问题分组

## 一、P0 级硬错误

这些问题不是“设计不佳”，而是当前运行时就会直接出错。

### 1. `_process_inbound_envelope()` 调用了不存在的 `_is_duplicate`

当前 `DingTalkAdapter` 初始化时创建的是：

- `self._dedup = MessageDeduplicator(max_size=1000)`

但 `_process_inbound_envelope()` 里调用的是：

- `self._is_duplicate(envelope.message_id)`

类中并没有 `_is_duplicate` 这个方法。

这会导致：

- 任何走到 `_process_inbound_envelope()` 的入站消息都可能直接抛 `AttributeError`
- 当前真实入口 `_IncomingHandler.process()` 正是调用这条链路

这类错误会让钉钉回调“已经到达 Hermes”，但 Hermes 自己在内部第一段处理就崩掉。

### 2. `disconnect()` 调用了不存在的 `_seen_messages`

当前类里已迁移到 `MessageDeduplicator`，但 `disconnect()` 中还残留：

- `self._seen_messages.clear()`

而 `__init__` 里根本没有 `_seen_messages` 字段。

这说明旧去重实现的残留代码没有被清干净，也意味着：

- 断连
- 重连前清理
- 适配器关闭

这些路径都可能产生额外的运行时错误。

### 3. 文件内存在两份 `_on_message()`

当前 `dingtalk.py` 中存在两个同名 `_on_message()`：

- 第一份旧版 `_on_message()` 调用不存在的 `_process_envelope(...)`
- 第二份新版 `_on_message()` 才是走 `_process_inbound_envelope(...)`

Python 运行时只会保留后定义的那一份，因此前一份是死代码。但死代码的存在本身说明：

- 文件经历过不完整的合并/重构
- 旧逻辑没有被收敛掉
- 后续维护时极易误导开发者

### 4. `_IncomingHandler.process()` 异常后仍返回成功 ACK

当前异常会被记录日志，但 handler 仍返回 OK。

这意味着：

- Hermes 内部处理失败
- 钉钉侧却认为消息已经被成功消费
- 某些消息会变成“静默丢失”

这不是主因，但它会明显放大主因造成的后果，让真实问题更难观察。

## 二、P1 级稳定性问题

这些问题最能解释你观察到的“`Hermes` 发图片有时候好、有时候坏”。

### 1. 图片/文件发送依赖 `session_webhook`，但调用方没有稳定透传

当前 `_send_payload()` 的行为是：

- 优先使用 `metadata["session_webhook"]`
- 否则退回 `_session_webhooks[chat_id]`

但在 `gateway/run.py` 和 `BasePlatformAdapter` 的很多媒体发送路径里，传给钉钉适配器的 metadata 只有：

- `thread_id`

没有：

- `session_webhook`

这就导致图片是否能发出去，很大程度取决于：

1. 当前 `chat_id` 对应的 webhook 有没有之前被缓存过
2. 适配器是否刚重启
3. 该用户/群聊是否刚好先发过一条入站消息
4. 缓存有没有被淘汰

这种隐式依赖最容易产生你说的那种“同样发图，有时候成功、有时候失败”。

### 2. `_session_webhooks` 只靠内存缓存，淘汰策略也很粗糙

当前缓存行为存在几个问题：

- 只存在内存中，不跨重启
- 键是否稳定依赖 `chat_id` 生成规则
- 容量打满后不是 LRU，而是简单弹出一个元素

这会导致：

- 某些活跃会话的 webhook 被挤掉
- 群聊和私聊的 `chat_id` 归一规则只要不稳，就会命中失败
- 服务重启后，在没有新入站消息前，主动发图可能全部失败

### 3. `_send_payload()` 成功判断过于乐观

当前逻辑只要 HTTP `< 300` 就认为发送成功。

但钉钉接口存在这种情况：

- HTTP 200
- body 里 `errcode != 0`
- 实际业务发送失败

如果不解析 body，只看 HTTP 状态，就会出现：

- 代码认为“已发送成功”
- 用户侧实际没收到图片

这也是发送行为不稳定、诊断困难的重要来源。

### 4. native 图片发送失败会 fallback 成 markdown 文本

`send_image_file()` 当前策略是：

- 优先走原生媒体上传 + 图片消息发送
- 任意异常则 fallback 为一条 markdown 文本

这个策略本身可以作为降级兜底，但当前的问题是：

- 上层如果只看 `SendResult.success`
- 很容易把“降级文本发成功”误当成“图片发成功”

也就是说，有些“图片发送成功”的表象，其实只是发了一条说明文字。

### 5. 上传与发送链路混用了新旧 DingTalk API 风格

当前实现里：

- token 与下载走 `api.dingtalk.com`
- 媒体上传走 `oapi.dingtalk.com`

这不一定马上错，但属于明显的兼容性风险点。只要钉钉某些租户、权限、产品线或开放平台策略发生差异，就会出现：

- token 正常
- upload 偶发失败
- 同一套参数在不同媒体类型上表现不一致

这也是“偶发不稳定”的潜在来源之一。

## 三、P2 级设计债务

这些问题暂时不一定直接崩，但会持续制造后续回归和维护困难。

### 1. 新旧入站逻辑没有彻底收敛

当前想走的是 raw-first 信封归一化架构，但旧的 `_on_message`、旧 dedup 残留、旧清理路径仍然存在。

这会导致：

- 当前文件难以被准确理解
- 修一个点容易碰坏另一个点
- 测试和实现之间不再一一对应

### 2. Hermes 的统一媒体发送链路对钉钉会话路由信息支持不够

`DingTalk` 适配器不是一个完全自由发送的平台，它的回复能力高度依赖会话路由信息。  
但 Hermes 的通用媒体发送路径没有把这种平台特性稳定建模出来，只透传了线程信息，没有稳定透传 reply webhook。

这说明不是 `dingtalk.py` 一份文件就能彻底解决，至少还要同步调整：

- `gateway/run.py`
- `gateway/platforms/base.py`
- 可能的 event/metadata 契约

### 3. 成功语义不够严格

对于媒体平台而言，“原生图片成功”、“文本降级成功”、“接口接受但业务失败”这三类结果不能混成一个简单的布尔值。

当前的返回语义会让：

- 监控误判
- 调用方误判
- 用户感知与日志状态不一致

## 根因总结

把这次问题压缩成一句话，可以这样描述：

**当前钉钉适配器的问题不是某个单独的图片 API 错了，而是“新旧逻辑混合 + 会话路由模型不完整 + 发送结果语义不严格”共同造成的系统性不稳定。**

如果只修 `_is_duplicate`，图片问题还会继续。  
如果只修图片上传，入站和断连问题还会继续。  
所以必须分阶段收敛。

## 修复目标

本次全面修复应达到以下目标：

1. `dingtalk.py` 中只保留一套入站逻辑
2. 入站、断连、重连路径不再包含已失效字段或方法
3. `Hermes` 给钉钉发图片/文件时，会话路由信息稳定可追踪，不依赖偶然缓存命中
4. 原生发送成功、降级发送成功、业务失败能被明确区分
5. 测试可以覆盖真实入口与关键交界面，避免下一次再被合并打坏

## 分阶段修复方案

## Phase 1：止血与收敛入站主链路（消息/图片/文件）

**目标：** 先让钉钉适配器变成只有一套可运行、可维护、不会马上炸的实现。

### 修复内容

1. 收敛去重机制
   - 统一使用 `MessageDeduplicator`
   - 删除对 `_is_duplicate` 的错误调用
   - 删除 `_seen_messages` 残留清理

2. 清理重复定义
   - 删除旧版 `_on_message()`
   - 删除 `_process_envelope` 相关残留路径
   - 保留唯一的 raw-first 入口

3. 修正断连清理
   - 只清理真实存在的字段
   - 确保 `_session_webhooks`、`_dedup`、token 状态一致

4. 校正 handler 与错误行为
   - 明确 `_IncomingHandler.process()` 的异常处理策略
   - 至少让错误日志和 ACK 语义可分析

### 交付标准

- 入站消息不再因结构性错误崩溃
- `disconnect()` 不再因残留字段报错
- 文件中不存在重复 `_on_message()`
- 钉钉相关测试至少能稳定运行到业务断言层，而不是先死于 `AttributeError`

## Phase 2：修复出站消息/图片/文件链路稳定性

**目标：** 解决“`Hermes` 给用户发图片有时成功、有时失败”的主问题。

### 修复内容

1. 明确会话路由模型
   - 在钉钉路径上稳定透传 `session_webhook`
   - 避免只依赖 `_session_webhooks` 内存缓存
   - 明确 `chat_id` 与 webhook 的绑定规则

2. 收敛发送成功判定
   - `_send_payload()` 解析响应体中的业务状态
   - `errcode != 0` 必须视为失败
   - 日志中记录 `errmsg`

3. 改善 fallback 语义
   - 区分：
     - 原生图片发送成功
     - 原生失败但 markdown 降级成功
     - 完全失败
   - 不再把所有情况都挤进单一 `success=True`

4. 改善 webhook 缓存
   - 改成 LRU 或更明确的策略
   - 增加命中/未命中日志
   - 防止高并发或多会话场景下偶发失效

5. 复核钉钉上传链路
   - 确认媒体上传接口是否应继续使用当前 `oapi` 方案
   - 若存在推荐的统一接口方案，考虑一并收敛

### 交付标准

- 同一会话内的图片/文件发送行为可预期
- 重启后只要有明确会话路由信息，媒体发送依旧稳定
- 发送失败时日志能明确区分是 webhook 缺失、upload 失败、还是业务 errcode 失败
- 用户侧“看起来发成功但其实没发图”的误判显著减少

## Phase 3：补强测试与可观测性

**目标：** 防止下一次类似合并/重构再次把钉钉适配器打坏。

### 修复内容

1. 增加真实入口测试
   - `_IncomingHandler.process()` 到 `_process_inbound_envelope()` 的链路测试
   - 避免只测 adapter 内部辅助方法

2. 增加媒体发送交界面测试
   - metadata 带不带 `session_webhook`
   - `_session_webhooks` 命中 / 失效 / 淘汰
   - HTTP 200 + `errcode != 0`
   - native 失败 + fallback 的结果语义

3. 增加结构一致性测试
   - 确保不存在重复核心方法
   - 确保关键入口不依赖已删除字段

4. 增强日志
   - 统一日志关键字，便于 grep 和线上排查
   - 明确打点：
     - webhook cache hit/miss
     - native upload fail
     - degraded fallback
     - inbound normalize fail

### 交付标准

- 核心回归能在测试阶段被拦下，而不是到线上才发现
- 出问题时可以快速定位是在：
  - 入站
  - webhook 会话路由信息
  - upload
  - send payload
  - fallback

## 建议实施顺序

推荐顺序是：

1. **先做 Phase 1**
   - 因为当前已存在确定的运行时错误
2. **紧接着做 Phase 2**
   - 因为这是直接影响用户体验的核心问题
3. **最后做 Phase 3**
   - 用来固化成果、防止回归

不建议只修一个点后就停止，因为：

- 只修入站，不会解决图片发送不稳定
- 只修图片发送，不会解决当前适配器文件结构脏乱与运行时错误

## 风险评估

本次修复的主要风险不在于改动量，而在于“会不会边修边把现有行为打散”。

主要风险包括：

1. 修改会话路由信息透传后，可能影响其他平台的通用媒体发送路径
2. 修改 fallback 语义后，可能影响上层调用方对 `SendResult` 的假设
3. 收敛 `dingtalk.py` 旧逻辑时，如果没有补足入口测试，容易误删仍被依赖的路径
4. 若钉钉上传接口策略发生变化，接口统一时要谨慎验证

因此建议：

- 先用最小可运行重构收敛主链路
- 每一阶段都要先补关键测试，再改实现
- 钉钉媒体发送相关改动尽量在一个小闭环内完成，不分散到太多无关文件

## 验证策略

修复完成后，至少需要覆盖以下验证：

### 自动化验证

1. `tests/gateway/test_dingtalk.py`
2. 新增 `_IncomingHandler.process()` 真入口测试
3. 新增 webhook 缺失/命中/淘汰测试
4. 新增 HTTP 200 + `errcode != 0` 测试
5. 新增 native 图片失败 + fallback 结果语义测试

### 手工验证

1. 钉钉私聊场景发图
2. 钉钉群聊场景发图
3. 紧跟入站消息回复图片
4. 适配器重启后再回复图片
5. 连续多个不同会话发送图片，观察缓存行为
6. 故意制造 upload 失败，看日志与用户侧表现是否一致

## 结论

当前 `DingTalk` 适配器的问题已经不是单个 bug，而是一次未完全收敛的重构后遗症。

最值得优先处理的不是“再补一个图片分支”，而是：

1. 先把适配器内部收敛成单一逻辑
2. 再把会话路由信息和媒体发送语义建模正确
3. 最后补齐交界面测试

只有这样，才能真正解决你现在观察到的：

- Hermes 发图片有时候成功、有时候失败
- 入站消息处理不稳定
- 修一个地方又冒出另一个地方的问题

---

如果接下来进入实施阶段，建议严格按：

- Phase 1 止血
- Phase 2 稳定发送
- Phase 3 补强测试

这个顺序推进，不要跳步。
