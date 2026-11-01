# PolyStrata 图像生成与动漫超分方案决策

- **决策状态：已批准（Accepted）；功能实现：待开发（Not implemented）**
- **确定日期：2026-11-01**
- **适用对象：PolyStrata 本地 Image Service，优先支持 Windows / RTX 3070 Ti 8GB**
- **核心原则：LLM as a Service；图像能力独立于文本模型；稳定优先于约 2 秒的并行收益**

## 一、确定的产品行为

| 项目 | 确定方案 |
|---|---|
| 第一方聊天内置生图默认分辨率 | **768×768**（取代现有工具的 512×512 默认） |
| 第一方 Images 页面默认分辨率 | **768×768**；允许用户明确选择其他合法尺寸 |
| 兼容性 | 保留现有 `POST /v1/images/generations` 对未显式指定 `size` 时的 **1024×1024** 行为，不破坏第三方 OpenAI Images API 客户端 |
| 文生图引擎 | 当前 Qwen-Image-2.1-Turbo GGUF + 无审查 Qwen3-VL 编码器，`sd-cli` Vulkan，8 步，CFG=1 |
| 动漫后处理 | **Real-ESRGAN `realesrgan-x4plus-anime`，4×，ncnn-Vulkan** |
| 动漫场景 | `upscale=auto` 时检测动漫/二次元风格，自动超分；其他场景默认不超分 |
| 默认动漫最终图 | 768×768 → **3072×3072** |
| GPU 策略 | **严格串行**：保存会话及 KV → 卸载已加载文本 LLM → 768 生图 → 图像引擎完全退出 → 动漫超分 → 超分引擎完全退出 → 热恢复 LLM → 返回结果并继续聊天 |
| 热恢复 | `gpu_policy=unload`，`reload=eager`；保持每个文本模型各自的 KV/会话资源配置，不混用跨模型快照 |
| 调度 | 整个生图与后处理持有既有共享 GPU/FIFO turn；禁止文本权重恢复与超分并发 |
| 失败回退 | 超分失败、超时或资源不足时返回 **768 原图**；不得因超分失败而使整次生图失败；最终始终尝试恢复之前加载的文本模型 |
| 文件 | 保留原图与最终图，聊天默认展示最终图；记录原图路径供查看和重新超分 |
| 推广范围 | **先对选定的本地部署配置 opt-in**，不全局修改上游默认，不与 Gemma/Qwen 具体模型绑定 |

> 本文是已经批准的**目标设计**，不是声称这些变更现在已经上线。当前聊天工具仍默认 512×512，原生 Images API 仍默认 1024×1024；自动 Anime 4× 尚未接入。

## 二、动漫自动识别（无额外视觉模型）

先读取用户请求的明确意图，再读取工具调用中的结构化风格提示及最终生图 prompt；不再为风格识别额外加载 CV/LLM 模型。

推荐在 `generate_image` 工具中增加：
- `size`：第一方默认 `768x768`；明确的用户尺寸优先。
- `upscale`：`auto | off | 4x`，默认 `auto`；用户明确关闭时不执行，明确开启时强制执行。
- `style_hint`：`auto | anime | other`，默认 `auto`。优先使用用户明确描述，其次模型推断，最后进行关键词兜底。
- `prompt`：完整生图提示词，继续必填。

**正例**：动漫、二次元、日式动画、anime、manga、cel-shaded anime、动漫立绘、动漫头像、初音/Miku 等明确动漫表达。

**不直接判定为动漫**：一般“插画/illustration”、卡通/cartoon、像素画、写实、照片、草图、水彩、3D 渲染；其风格不明确时默认不超分，避免误判。

优先级：

1. 用户明确提出 `upscale=off` → 不超分。
2. 用户明确要求 `upscale=4x` → 执行（在服务端的资源与尺寸限制内）。
3. 否则，若用户明确指向动漫风格，或 `style_hint=anime` 且无用户反向指令 → 自动 4×。
4. 其他 → 保留原图。

**安全上限**：自动 4× 限制默认输出最长边不超过 **3072px**；若用户显式选择大于 768 的底图且自动放大后会超限，自动跳过并标明原因。用户明确要求 4× 时应再检查实际允许的最大尺寸、可用磁盘和超分器是否支持；不允许不受控的大尺寸任务。

## 三、GPU 与恢复时序（必须遵守）

```text
用户请求
  ↓
模型判定需生图、形成 prompt + style_hint + upscale
  ↓
获取共享 GPU FIFO turn（锁覆盖完整流程）
  ↓
文本 LLM 当前已加载？
  ├─ 是 → 保存会话、KV 快照；卸载 LLM，等待显存释放
  └─ 否 → 不额外加载/卸载 LLM
  ↓
运行 Qwen-Image（默认 768×768，8 steps）
  ↓
等待 sd-cli 进程完全退出，释放资源；保留 768 PNG
  ↓
动漫自动判定且符合尺寸上限？
  ├─ 是 → 独占运行 Real-ESRGAN Anime 4×；等待退出
  │       ├─ 成功 → 最终图 3072×3072
  │       └─ 失败 → 最终图回退为原始 768 PNG
  └─ 否 → 最终图为原始 PNG
  ↓
之前 LLM 已加载？
  ├─ 是 → 热恢复原 LLM + 原模型的会话/KV（不可复用则重新 Prefill）
  └─ 否 → 继续保持卸载状态
  ↓
释放 GPU turn → 将最终图和状态送回原聊天会话
```

**不启用并行**：即使短上下文试验中的“Gemma 恢复与超分同时启动”连续 3 次成功，最长上下文、KV 扩张及 Windows WDDM 显存波动尚未被充分覆盖，约 2 秒加速不值得引入额外的不确定性。

特别注意当前 `serve/images.py::gpu_turn` 的 `_give_back()` 在上下文退出时触发 eager 恢复：**实现时必须把超分放在该上下文内部**，不能写成 `gen.run() → 离开 gpu_turn → upscale()`，否则 Gemma 会提前恢复。

## 四、失败处理和可观察性

- Qwen 生图失败：返回明确错误；通过 `finally` 恢复原 LLM（已加载时），避免 GPU/服务长期停在卸载状态。
- 超分失败、非零退出、超时、OOM、设备丢失、取消：尽可能结束/回收超分子进程；如果原图已生成，**返回 768 原图** 并附提示，不重跑昂贵的 Qwen-Image。
- KV 恢复失败：保留会话历史；按既有机制重新 Prefill。恢复 LLM 失败也要保留图像结果与错误提示，不吞掉异常。
- 若在执行前文本模型原本没有加载，不应因图像任务结束而无条件重新加载它。
- 图片文件与聊天引用应保持一致；清理旧文件不能删除仍被持久聊天引用的图片。
- 单任务记录：`queue_wait_s`、`llm_snapshot_s`、`llm_unload_s`、`image_init_s`、`text_encode_s`、`diffusion_s`、`vae_decode_s`、`image_total_s`、`upscale_s`、`llm_reload_s`、`overall_s`、`upscale_applied`、`fallback_reason`；显存/系统 RAM 峰值和失败原因。日志不得输出 API Key、密钥或机密提示词原文。
- 按 `Images API` 和聊天工具统一共享后处理后端，保障 API 结构兼容；原始 PNG 可通过已有安全文件路径访问。

## 五、当前硬件的实测依据（非性能承诺）

在 Windows 10、RTX 3070 Ti 8GB、i7-12700、64GB RAM，使用下载好的 Qwen-Image GGUF 与 Real-ESRGAN ncnn-Vulkan：

| 阶段 | 单次实测耗时 | 整卡显存占用峰值 |
|---|---:|---:|
| Gemma 卸载 | **0.830 秒** | 6,265 MiB（开始时仍驻留） |
| Qwen-Image 768×768 | **24.533 秒** | 4,600 MiB |
| Real-ESRGAN Anime 4× | **2.598 秒** | 578 MiB |
| Gemma 热恢复 | **2.269 秒** | 6,229 MiB |
| **完整串行流水线** | **30.418 秒** | — |

Qwen-Image 测得条件编码 6.45 秒、8 步采样 12.23 秒、VAE 解码 3.50 秒。超分输出 3072×3072，文件 11,444,998 bytes。采样数值来自 `nvidia-smi`，WDDM/采样间隔下不是绝对峰值。

并行试验的 3 轮关键阶段为 2.8115 / 2.7845 / 2.7669 秒，整卡显存峰值均 6,806 MiB；**只作为排除/对照方案的数据，不改变本文件的串行决策**。

原始实验记录（本机）：
- `E:\LLM\deploy\benchmark\qwen-image-eval\upscale-benchmark\RESULT_768_ANIME4X_20261101.md`
- `E:\LLM\deploy\benchmark\qwen-image-eval\upscale-benchmark\PARALLEL_RELOAD_UPSCALE_REPORT_20261101.md`
- `E:\LLM\deploy\benchmark\qwen-image-eval\upscale-benchmark\measurements.json`
- `E:\LLM\deploy\benchmark\qwen-image-eval\upscale-benchmark\telemetry.csv`

## 六、后续实现拆分和验收标准

1. `serve/image_tools.py`：第一方聊天默认 `768x768`，新增 `upscale` 和 `style_hint`；所有入参后端校验，不能只信任模型输出。
2. `serve/upscale.py`（新模块）：封装 Real-ESRGAN 4×、风格解析、尺寸/资源检查、子进程超时与取消、失败回退；独立进程退出释放显存。
3. `serve/images.py`：将原图、超分后图像与阶段测量整合进既有 `gpu_turn`，确保 eager 热恢复在超分结束之后。
4. `serve/web/app.js`：第一方 Images UI 默认 768；展示最终图片、原图和降级提示；保留现有 API 行为与鉴权。
5. 可配置的独立 ImageBackend/Upscaler，Gemma、Qwen 使用同一套部署服务、各自 KV 快照名额，避免给模型写专用分支。

**验收必须包含**：
- 明确动漫请求：768→3072，4× 成功；写实请求：保持 768；`upscale=off` 完全跳过。
- 显式尺寸、非动漫关键词、冲突风格提示、超大尺寸均按优先级与上限处理。
- 强制让超分返回错误/超时/取消：原 768 图可交付，LLM 恢复和队列不被阻塞。
- 长上下文及 KV 保存/加载失败回退验证；不同模型切换后恢复原模型，不串 KV。
- 多个聊天请求竞争同一 GPU 时严格串行、无死锁；模型原本卸载时不额外加载。
- Web 生成图可正常查看；API 不开启选项时与旧版响应行为兼容。
- 复测耗时、GPU 峰值、系统 RAM 峰值及时间分段；原始样本留档。

### 禁止事项

- **禁止**启动 Gemma 热恢复的同时运行 Anime 超分。
- **禁止**在超分之前退出 GPU turn，让 eager 恢复抢先运行。
- **禁止**由于超分失败丢失已成功生成的原图。
- **禁止**为生图功能改动模型专有 KV 资源模型或把 ImageBackend 绑定到某个 LLM。
- **禁止**把本次目标设计误写成当前已上线功能。
