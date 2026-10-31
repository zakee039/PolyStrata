# PolyStrata Manager 与 Workflow Prefix Cache 开发计划

> 状态：P1 Windows 原生托盘管理器 v0.1.2 已实现并通过启动/停止自检；配置文件选择与只读详情已归入第二页；Workflow Prefix Cache 仍为后续开发计划
> 日期：2026-10-31
> 目标平台：Windows 10 / Windows 11；第一阶段适配当前 MeroMero + RTX 3070 Ti 部署。
> 项目目录：E:\LLM\deploy\polystrata
> 范围：规划与界面设计，不代表对应功能已实现。

## 1. 目标与边界

为 PolyStrata 增加轻量级常驻托盘管理器，统一监控、守护、启动/停止、模型注册和参数设置，同时在 PolyStrata Server 中实现独立的 Workflow Registry 与 Workflow Prefix Cache。Miku Harness 等客户端可注册稳定工作流前缀、显式选择工作流，由服务端选择可复用 KV，减少重复 Prefill。

原则：
- 托盘管理器只负责用户交互和服务进程监管，推理及缓存逻辑必须独立运行于 PolyStrata Server / C++ Engine。
- 保留原 WebUI；Manager 的 Chat 快捷入口打开原 WebUI，不再开发第二套聊天界面。
- 取消原拟议“GPU 卸载但模型状态强制留在 RAM 中”的独立临时暂停模式；不做无实测收益的 RAM 热待机。
- 模型自动空闲卸载与用户手动停止必须区分，服务守护不得违背用户的停止意图。
- Workflow 是持久配置，KV 是可删、可重建的加速产物；与 SQLite 对话、普通 KV 快照分别管理。
- 本阶段不修改现有模型文件、线上部署配置和已运行的推理服务。

## 2. 当前已实现能力（2026-10-31 校对）

- 启动：`run-meromero-128k-int8.bat`，API `127.0.0.1:18081`。
- WebUI：`serve/web`；Chat、Monitor、About；原生接口提供 `GET /metrics`、`GET /api/models`、`POST /api/models/load`、`POST /api/models/unload`、`POST /unload`、`POST /load`、`GET/POST /config`。
- 统一会话：`serve/session_v1.py`，SQLite 对话存储、图片/附件持久化、最多 5 份普通会话磁盘 KV 快照（现为代码内固定值）。
- C++ 原生：`src/poly/session_disk.hpp`、`src/poly/conversations.hpp`；支持文本会话 SAVE/RESTORE，校验模型及 KV 格式，存储 token、循环状态和 attention rows；RESTORE 会重置内部驻留会话槽位。
- 当前部署：`idle_unload_s = 900` 秒；`--conversation-cache-mib auto`，默认 RAM parked slots 上限 4；`session_v1_kv = true`。
- 当前 `POST /unload` 终止原生推理引擎、释放其 GPU/RAM，下次请求自动重载；不是持 RAM 热待机。
- `serve/models.py` 的发现机制按配置目录 `strata-*.json` 扫描；当前 128K 生产配置放在 `E:\LLM\deploy\benchmark\configs`，多目录模型发现需要扩展。
- 工作流注册/预热/调用接口**尚不存在**。现有 SAVE 在会话检查点保存，不能直接保证恰好截断于目标工作流固定前缀。

## 3. 组件职责

### PolyStrata Manager（拟新建桌面项目）
- 推荐 Tauri 2 + Rust + 小型 React/TypeScript 前端；常驻托盘，打开弹窗时才挂载图形界面。
- 唯一服务生命周期管理入口：管理启动、优雅停止、进程树回收、意外故障限速重启。
- 从服务 API 与本机系统遥测读取状态，避免在 Manager 再实现推理逻辑。
- 不读取或改写 KV 二进制；通过受限本地 API 配置工作流并展示状态。

### PolyStrata Server
- 现有 API、认证、聊天历史、模型注册、Prefill/Decode 统计的权威来源。
- 新增 Workflow Registry：版本、定义、固定前缀 Token 化、模型关联、状态与持久化。
- 新增 Prefix Cache Scheduler：优先可直接复用的有效 RAM 会话；比较磁盘恢复成本与预计重新 Prefill 成本；不匹配则常规 Prefill。
- 保存/恢复请求必须经过现有单槽 FIFO 和会话锁；不能并发破坏其他请求。

### C++ Engine
- 扩展专门的 Prefill-only / exact-prefix-checkpoint 原语，在精确 Token 边界产生 KV、循环状态及 attention rows。
- 原 SAVE/RESTORE 的签名、完整性验证、失败降级继续保留；调整恢复后 RAM parked slots 的代价并计入调度决策。
- V1 只承诺文本固定前缀 KV，图像、多模态后续单独研究。

## 4. 用户可见 UI 结构（待定稿）

尺寸建议：约 420 × 560～640 CSS px；亮色清洁界面，薄荷绿 #39C5BB 作为功能强调色，支持暗色模式；克制图形，不做大面积渐变或复杂动画。

### 主面板 / 首页
- 顶部：PolyStrata 标识、当前模型下拉、连接状态（运行中 / 生成中 / 15 分钟空闲已卸载 / 正在载入 / 已停止 / 故障）。
- 主要指标：① 本进程 GPU 显存用量与整卡显存使用量（明确区分）② Decode tok/s ③ Prefill tok/s ④ 输入/输出 Token 累计，另展示缓存命中 Token 与复用率。
- 指标语义：Prefill 速率 = 实际新处理的 Token / Prefill 耗时；Decode = 生成 Token / Decode 耗时；空闲时展示“上次请求值”，不得假装实时运行中。总 Token 统计需标明“本次启动”或“持久累计”。
- 主操作：运行/重新载入、停止服务（操作意图明确，必要时确认）；独立“打开 Chat”快捷按钮。
- 底部入口：状态与请求详情、模型管理、工作流缓存、设置、日志；高级指标放二级页面。
- 托盘右键菜单：打开面板、打开 Chat、启动/停止服务、当前模型、退出 Manager。图标颜色/状态可快速识别运行、忙碌、自动卸载、已停止、异常。

### 状态详情
- GPU 物理使用与服务估计占用、RAM、显存温度、推理状态、Prompt/Reuse/Decode 统计和最近请求。
- 显示 `Prefill`、`Decode`、`TTFT`、已复用 token、KV 从 RAM / 磁盘恢复时间（如有）、队列长度。
- 监控不应产生大量磁盘写；正常轮询 1～2 秒，最小化后台 CPU 与网络开销。

### 模型管理
- 动态从注册目录发现模型和配置，并显示路径、架构、量化、上下文、是否支持视觉、状态。
- 操作：注册/移除配置记录、选择默认模型、切换、查看模型专属参数；先验证路径、模型兼容及 Tokenizer。
- 注册来源支持多个目录和手动导入配置。部署在 benchmark/configs 的配置也需能被管理器识别。
- “注册模型”不是复制大 GGUF 文件；始终允许模型在原位置。

### 参数设置
- 服务：API 地址/端口（默认仅回环）、守护开关、意外退出自动重启策略、开机启动、自动打开 WebUI。
- 资源：空闲自动卸载时间（当前 900s，0 关闭）、VRAM 保留、RAM parked conversation MiB / slots（目前 4）、普通对话磁盘 KV 最多份数（目前硬编码 5，需参数化）、磁盘预算。
- 所有设置明确显示“立即生效 / 重载模型生效 / 重启服务生效”；备份后原子写入、验证失败回滚。

### 工作流缓存
- 每模型的 Workflow 列表：名称、ID、注册版本、前缀 token 数、快照大小、绑定模型、最近命中、状态、最后预热时间。
- 状态：未注册 / 已注册未预热 / 正在构建 / 已就绪 / 内容变化需重建 / 模型不兼容 / 构建失败。
- 操作：注册/更新、预热/重建、启用/禁用、删除缓存、删除注册定义、查看命中统计。
- 固定工作流快照不参加普通 5 份会话 KV FIFO，独立目录及独立存储配额。
- Manager 可供人工操作；注册能力应当也是 API 原生能力，不依赖图形界面运行。

## 5. 生命周期和守护策略

状态分离为：Manager 运行与否、Python API 运行与否、C++ 引擎已加载与否、实际请求忙闲。

- “运行”：API 在线且可按需加载模型。当前无推理时显示就绪。
- “15 分钟自动卸载”：在 FIFO 中先尝试保存有效普通会话 KV，再卸载引擎 GPU/RAM；API 仍在线；新请求自动唤醒模型并尝试恢复兼容 KV。
- “手动停止服务”：先拒绝新请求并优雅处理进行中请求（由用户选择等待/取消），保存 KV，再退出 Python API 与推理子进程树；释放可控 RAM/GPU；Manager 继续驻留，但不自动启动；手动启动才能恢复。
- “意外故障”：按指数退避与次数上限执行守护重启，记录故障原因，连续失败转故障状态，不无限重启。
- “退出 Manager”：可设置是否保持已托管服务，但默认采用不会遗留孤儿进程的行为并明确提示。
- 所有启动/停止操作应有幂等处理与防双实例锁；防止重复启动多份 8GB 显存模型。

## 6. Workflow Prefix Cache：注册、预热、调用

### 三个独立动作
1. **注册工作流**：Miku Harness（拥有原始 System Prompt、稳定工具定义）或 Manager 提交版本化固定内容，服务端保存副本及前缀元数据；重复内容幂等。
2. **注册并预热**：服务端对精确固定前缀做一次 Prefill-only；建立模型/格式/Token 指纹并原子保存只读快照。允许后台排队，但不能与当前推理并发破坏单槽引擎。
3. **调用工作流**：Miku Harness 按 workflow ID 正常提交增量对话；服务端拼接固定模板，并按成本/兼容性选择复用缓存或正常 Prefill。

### 拟议接口（未实现）
- `PUT /v1/strata/workflows/{id}`：注册/更新工作流定义，支持版本/幂等。
- `GET /v1/strata/workflows`、`GET /v1/strata/workflows/{id}`：列出和查询状态。
- `POST /v1/strata/workflows/{id}/warm`：队列化预热，提供任务状态与进度。
- `DELETE /v1/strata/workflows/{id}/cache`：只删除加速缓存。
- `DELETE /v1/strata/workflows/{id}`：删除注册定义（受权限约束），不得误删普通对话。
- 兼容 `POST /v1/chat/completions` 新增可选字段 `strata_workflow_id`；客户端按需保留 `strata_session_id`。
- 不支持自定义字段的客户端：可考虑注册虚拟别名 `model@workflow`，作为后续兼容扩展，不在第一阶段强制做。
- 注册与管理接口只允许受认证的本机管理客户端；不应因允许本地管理而打开不安全的跨站控制。

### KV 正确性约束
- 固定前缀包括：System Prompt、工具声明、角色边界、聊天模板和影响 Token 化的可见字符串。动态时间、屏幕、记忆检索结果、会话历史放到后续动态部分。
- 保留两层校验：模型/引擎/KV 兼容指纹 + 精确渲染的 Token ID 前缀哈希；真实生成 Token 前缀不匹配绝不强行 RESTORE。
- 模型、Tokenizer、模板、影响状态的计算配置发生变化时自动失效；无关的采样温度修改不必无意义地重建（具体规则须由引擎兼容性验证）。
- 首次调用无缓存：正常 Prefill，安全时自动生成固定前缀缓存；若无法安全产生 exact-prefix checkpoint，则异步预热或下次创建，绝不从错误边界截取。
- 如已有更长的同会话 RAM KV，优先考虑继续复用；不能因为工作流模板更“固定”就破坏高价值 RAM 缓存。
- 当前 RESTORE 清空 parked slots；缓存调度必须理解此副作用。若 Restore 总成本高于重新 Prefill，则直接 Prefill。
- WORKFLOW KV 独立保留与版本管理；普通对话 KV 保留各自 FIFO；磁盘快照可安全被删后重建。文本 V1 不宣称视觉快照已支持。

### Miku Harness 集成
- 模块设置页提供“注册并预热”按钮（例：`miku-main`、`miku-memory`、`miku-planner`）。
- Harness 是工作流定义权威来源；内容变化后重新 PUT 注册；状态“已就绪 / 需重建 / 不支持 / 失败”由 PolyStrata API 返回。
- 日常请求只携带 `strata_workflow_id` 和动态消息。云端 Gemini 不支持读取本地 PolyStrata 的 KV，按钮应明确标明“仅本地 PolyStrata 模型可用”。
- 动态注入图片、记忆检索结果、实时观察、任务进度不会修改固定前缀；但 V1 视觉多模态 KV 需要另外实现。

## 7. 文件布局（建议，不是已存在路径）

- `manager/`：Tauri 2 + Rust + 前端。
- `serve/workflows.py`：注册、定义版本、权限和持久化。
- `serve/prefix_cache.py`：选择、预热队列、成本估计、失效管理。
- `workflow-cache/`：每个模型/版本的独立 KV，忽略 Git。
- `docs/MANAGER_WORKFLOW_PLAN.md`：本计划。
- 具体的原生 C++ 预热命令放在 `src/poly`，须覆盖 Windows CUDA 的回归测试。

## 8. 实施里程碑

### P0 详细 UI 设计
- 固定弹窗尺寸、导航结构、各个状态文字/颜色、操作反馈、错误状态、最小状态数据协议。
- 创建静态 UI 原型，优先用现有服务数据 mock 验证信息密度和操作路径。

### P1 Manager 最小可用版
- 托盘单实例、状态轮询、打开 WebUI、服务启动/停止、进程守护、防重复启动、日志查看。
- 复用 `GET /metrics` 显示 Decode、Prefill、KV 复用和资源占用；不改推理算法。

### P2 模型与设置
- 多目录模型注册、模型选择与配置管理、RAM slots / 空闲卸载 / 普通 KV 份数参数化。
- 修改配置不破坏正在进行的推理；需重启项目显示提示；有回滚。

### P3 Workflow Registry + Prefix Cache
- 注册/更新/查询 API、严谨的 Token 边界、Prefill-only、快照安全保存、指纹校验、按需恢复。
- 接入 Miku Harness“注册并预热”按钮与 `strata_workflow_id`。

### P4 性能与回归
- 单模块 1K/4K/16K/32K 固定前缀：比较冷 Prefill、热 RAM 重用、磁盘 KV 恢复与首 Token 延迟（TTFT）。
- 校验并发/队列、变更 Tokenizer、更新模型、异常文件、闲置卸载重启、五份普通会话快照保留、RAM park slots 未被错误清空。
- 对同一 prompt 在缓存命中/未命中下核对生成 Token 一致性或明确指出非确定性边界。
- 不修改既有不相关代码，不删除用户模型、历史对话和备份；保留独立回滚方案。

## 9. 明确暂不做

- RAM 强制热驻留及单独“临时暂停”按钮。
- 在托盘内部重写 Chat 或复杂的任务/Agent 编排 UI。
- KV 跨模型共享或将 KV 当长期知识库。
- 不经严格前缀验证的“模糊匹配”恢复。
- 直接声明视觉/多模态 KV 已实现。
- 未经测试宣称固定加速倍数或冷启动耗时。

## 10. 待定界面问题（下一轮讨论）

- 主窗口默认显示 4 张指标卡还是更紧凑的横向两行指标；是否加入一条 Token 速度趋势图。
- 首页操作是单主按钮随状态切换“启动/停止”，还是两个独立按钮。
- 二级入口在底部导航（状态/模型/工作流/设置）还是分组列表。
- “服务守护”和“退出时是否停止服务”的默认选项；交互确认与托盘菜单。
- 工作流列表是在 Manager 中作为独立页还是模型详情中的子页。

## 11. 验收基准

- 后台托盘可运行但不启动模型；关闭面板不退出托盘；托盘退出/服务停止语义清楚。
- 运行、15 分钟自动卸载、手动停止、异常重启状态不混淆。
- 同一进程生命周期中无重复引擎；守护不重启用户明确停止的服务。
- 不接触未授权端口，不开放无鉴权的远程控制接口。
- 固定前缀匹配才使用 KV；工作流与普通会话 KV 相互隔离；错误指纹时安全降级。
- 统计展示有单位、时间范围和真实来源，不拿示例值冒充设备实测值。
- UI 和新增功能默认不改变原推理路径；开启功能与关闭功能均有回归测试。
