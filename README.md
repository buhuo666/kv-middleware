# llama.cpp KV Prefix Cache Middleware

[English](README.en.md) | 简体中文

**OpenAI 兼容 Agent 的可持久化 llama.cpp KV 前缀缓存。**

[技术设计](docs/TECHNICAL.zh-CN.md) | [English technical documentation](docs/TECHNICAL.en.md) | [贡献指南](CONTRIBUTING.md) | [安全策略](SECURITY.md) | [MIT License](LICENSE)

一个位于 OpenAI 兼容 Agent 与 `llama.cpp` 之间的 KV 前缀缓存中间件。

它自动从 Agent 请求中识别较稳定的系统提示词、开发者提示词和工具列表，为这些内容建立独立的 KV 前缀快照。后续新会话使用相同模板时，中间件会先把快照恢复到 `llama.cpp` slot，再只处理新增的会话内容，从而减少重复预填充。

> 当前版本属于实验性实现。部署前请阅读“适用条件与限制”，并使用自己的模型、上下文参数和 Agent 请求进行基准测试。

## 主要目的

许多 Agent 会在每个新会话开头发送大量重复内容，例如：

- 系统提示词和开发者提示词；
- 工具定义及其 JSON Schema；
- Agent 固定能力说明；
- 基本不随会话变化的运行规则。

这些内容可能占用数千甚至数万个 Token。`llama.cpp` 进程重启后，显存中的 KV 会消失；仅依靠进程内缓存，第一次请求仍需重新预填充。本项目利用 `llama.cpp` 的 slot 保存和恢复接口，将可复用前缀保存为磁盘快照，并在需要时恢复。

## 实测效果

在公开记录的一组 Hermes Agent 实测中，保留中间件生成的 KV 快照，重启 `llama.cpp` 并新建对话后：

> **平均模型处理时间从 91.0 秒降至 13.7 秒，减少 77.3 秒，耗时降低约 85.0%。**

| 场景 | 三次模型处理时间 | 平均值 |
| --- | --- | ---: |
| 透明路由冷启动 | 1:46、1:24、1:23 | **91.0 秒** |
| 重启模型后恢复已保存 KV | 0:12、0:13、0:16 | **13.7 秒** |

这组结果表明，中间件可以把稳定的系统提示词、工具列表等 Agent 前缀保存到磁盘，并在模型重启和新建对话后恢复使用。首次建立 KV 的平均时间为 1:29.0，主要收益出现在后续恢复阶段。

完整测试流程、测试环境、结果截图和限制说明：

- [KV 前缀缓存实测效果（中文）](docs/benchmarks/kv-prefix-cache/README.md)
- [KV Prefix Cache Measured Results (English)](docs/benchmarks/kv-prefix-cache/README.en.md)

```text
OpenAI-compatible Agent
          |
          | /v1/chat/completions
          v
KV Prefix Cache Middleware ---- metadata/templates ---- ./data
          |
          | restore / route / save
          v
      llama.cpp server -------- KV snapshots -------- slot-save-path
```

## 功能

- 暴露 OpenAI 兼容的 `/v1` 接口，Agent 无需使用专有请求格式；
- 自动提取连续位于消息开头的 `system`/`developer` 消息和 `tools`；
- 根据规范化后的模板内容计算唯一前缀 Hash；
- 在请求用户问题前单独预填充并保存前缀，避免把对话内容写入通用快照；
- 模型重启后自动识别空 slot，并从磁盘恢复匹配的 KV 快照；
- slot 不足时淘汰空闲的内存绑定，但保留旧快照供下次恢复；
- `--parallel 1` 下对 KV 请求排队，防止并发 Agent 相互覆盖同一个 slot；
- 支持普通和 SSE 流式的 Chat Completions；
- 在同一个启动终端中提供管理命令、Tab 补全和中英文显示；
- 提供简单的只读 Web 状态页和管理 API。

## 工作环境

### 必需组件

- Python 3.10 或更高版本；
- 支持 HTTP Server 的较新版本 `llama.cpp`；
- 一个可被 `llama-server` 加载的 GGUF 模型；
- OpenAI 兼容的 Agent 或客户端。

### llama.cpp 接口要求

所使用的 `llama.cpp` 构建需要支持以下接口：

- `/health`
- `/props`
- `/slots`
- `/slots/{id}?action=save`
- `/slots/{id}?action=restore`
- `/apply-template`
- `/completion`
- `/v1/chat/completions`

项目本身可运行于 Windows、Linux 和 macOS。实际推理设备、CUDA/ROCm/Metal 环境及模型参数由 `llama.cpp` 决定。

## 安装

### 1. 获取项目

```bash
git clone https://github.com/buhuo666/kv-middleware.git
cd llama-kv-middleware
```

### 2. 创建虚拟环境

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
# 或者以可编辑项目方式安装：python -m pip install -e .
```

Linux/macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3. 创建本地配置

Windows PowerShell：

```powershell
Copy-Item config.example.yml config.yml
```

Linux/macOS：

```bash
cp config.example.yml config.yml
```

`config.yml` 已被 `.gitignore` 排除。请不要把包含真实主机名、端口、路径或密钥的本地配置提交到仓库。

## 启动 llama.cpp

若要在模型重启后继续复用 KV，必须为 `llama-server` 设置 `--slot-save-path`：

```text
llama-server \
  -m /path/to/model.gguf \
  --host 127.0.0.1 \
  --port 8080 \
  --ctx-size 32768 \
  --parallel 1 \
  --slot-save-path /absolute/path/to/slot-cache \
  --metrics
```

Windows 可使用相同参数启动 `llama-server.exe`，并把模型及快照目录替换为本机绝对路径。

关键参数说明：

| 参数 | 作用 |
| --- | --- |
| `--slot-save-path` | 允许 slot KV 保存到磁盘和从磁盘恢复；配置文件必须填写同一个目录。 |
| `--parallel 1` | 当前版本推荐配置。只有一个 slot 时，中间件会让缓存请求排队并按需切换快照。 |
| `--ctx-size` | 必须容纳前缀、当前会话和生成内容；过小会导致请求失败。 |
| `--metrics` | 便于通过指标确认已处理和已缓存的 Token 数量。 |
| `--cache-type-k/v` | 可按显存情况选配；建立与恢复快照时应保持一致。 |

模型路径、模型文件、GPU 分层参数和采样参数不属于中间件配置，请根据本机环境设置。

## 配置

编辑本地 `config.yml`：

```yaml
llama_cpp:
  base_url: "http://llama-cpp-host:8080"
  timeout_seconds: 300
  slot_save_path: "/absolute/path/to/slot-cache"

agent:
  host: "127.0.0.1"
  port: 8000
  api_base: "http://127.0.0.1:8000/v1"

storage:
  data_dir: "./data"
```

注意：

- `slot_save_path` 必须与 `llama-server --slot-save-path` 指向同一绝对目录；
- `data_dir` 保存 KV 元数据和模板参考文件，不保存聊天历史；
- `config.yml` 仅在中间件启动时读取，运行期间不会自动改写；
- 也可使用 `KV_CONFIG` 指定其他 YAML 文件；
- `LLAMA_BASE_URL` 和 `KV_DATA_DIR` 可覆盖 YAML 中的对应值。

## 启动中间件

确保 `llama-server` 已就绪，然后在项目目录运行：

```bash
python middleware.py
```

正常启动后终端会显示：

```text
llama.cpp KV Middleware 0.3.0
Agent API: http://127.0.0.1:8000/v1
kv>
```

这个终端同时是管理控制台。不要再启动第二个 `middleware.py` 实例占用同一端口。

健康检查：

```text
GET http://127.0.0.1:8000/health
GET http://127.0.0.1:8000/upstream/health
```

状态页：

```text
http://127.0.0.1:8000/admin
```

## Agent 接入

将 Agent 原本指向 `llama.cpp` 的 OpenAI Base URL 改为中间件地址：

```text
Base URL: http://127.0.0.1:8000/v1
API Key:  任意非空占位值（若客户端强制要求）
```

常用接口：

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/completions`
- `POST /v1/embeddings`，取决于上游模型和 `llama.cpp` 构建是否支持。

普通 Agent 不需要主动指定 KV ID。中间件会从请求中识别稳定前缀，自动建立、匹配和恢复快照。高级客户端也可以携带：

```text
X-KV-Prefix-ID: <prefix-id>
X-KV-Slot-ID: 0
```

显式指定时，请求中的系统/开发者消息和工具列表必须与该 KV 模板完全匹配，否则中间件返回 HTTP 409，避免使用错误缓存。

## 缓存生命周期

### 第一次看到新模板

1. 中间件提取系统/开发者消息和工具列表；
2. 调用 `/apply-template` 获得模型实际使用的渲染前缀；
3. 使用 `n_predict: 0` 只预填充前缀；
4. 调用 slot save 接口写入磁盘快照；
5. 再发送包含用户问题的正式请求。

第一次请求仍需付出建立前缀的预填充成本，这是生成可复用快照所必需的。

### 相同模板的新会话

如果 slot 中已经是目标 KV，则直接复用；如果 slot 已被其他模板占用，则先从磁盘恢复目标快照，再处理用户新增内容。

### slot 不足时

当前 slot 空闲时，中间件会移出旧的内存绑定并装入新模板。旧 KV 的元数据、模板 JSON 和 `.bin` 快照不会删除。旧 Agent 再次请求时会从磁盘恢复。

正在推理的 slot 不会被强制替换。使用 `--parallel 1` 时，不同缓存请求会依次执行。

## 如何确认 KV 已复用

在管理终端输入：

```text
logs
messages
```

重点观察以下事件：

| 事件 | 含义 |
| --- | --- |
| `prefix_cache_built` | 新前缀已经预填充并建立快照。 |
| `prefix_slot_evicted` | slot 切换到其他模板，旧磁盘快照仍保留。 |
| `prefix_cache_restored` | 已从磁盘恢复目标前缀。 |
| `prefix_cache_reused_in_memory` | slot 中已经是目标前缀，无需磁盘恢复。 |
| `prefix_cache_result` | 模型最终报告的缓存命中结果。 |
| `prefix_restore_failed_fallback` | 恢复失败，当前请求进入回退或重建路径。 |

最可靠的判断是模型响应中的：

```json
{
  "usage": {
    "prompt_tokens_details": {
      "cached_tokens": 12000
    }
  }
}
```

`cached_tokens` 应接近保存的前缀 `token_count`。仅看到 restore 日志不代表最终一定命中，仍应以 `prefix_cache_result` 和 llama.cpp 返回的统计为准。

不同 llama.cpp 构建版本暴露缓存计数的位置可能不同。中间件会优先读取
OpenAI 的 `usage.prompt_tokens_details.cached_tokens`，其次读取兼容的
`usage` 字段，最后读取 llama.cpp 常见的 `timings.cache_n`/`n_cache` 字段。
如果上游没有返回任何缓存计数，命令行会显示“未知”，不会根据 restore 请求自行猜测命中。

启用 `--metrics` 后，也可查看：

```text
llamacpp:prompt_tokens_total
llamacpp:prompt_tokens_cached_total
```

## 管理命令

命令名不带短横线，只有选项带 `-`。在 `kv>` 后输入命令，按 Tab 可补全。

| 命令 | 说明 |
| --- | --- |
| `help` | 显示帮助和参数。 |
| `version` | 显示版本信息。 |
| `status` | 查看服务、上游和缓存状态。 |
| `config` | 查看启动时读取的脱敏配置。 |
| `show` | 查看中间件功能、路由和错误状态。 |
| `list` | 仅列出 KV ID、状态和创建时间。 |
| `list -f <值>` | 按 KV ID、名称、Agent ID、文件名或日期筛选。仅命中一条时显示详情。 |
| `del -f <值>` | 删除匹配 KV 的元数据、模板和磁盘快照。 |
| `del all` | 明确删除全部 KV；此操作不可撤销。 |
| `messages` | 查看本次中间件进程收到的请求和响应摘要。 |
| `messages -f <序号>` | 查看单条消息详情。 |
| `messages -f <序号> <列标题>` | 只查看指定列。 |
| `messages clear` | 清空内存中的消息记录。 |
| `template` | 查看 Agent 模板参考文件。 |
| `template -f <文件名或ID>` | 查看单个模板。 |
| `template del <文件名或ID>` | 删除模板参考信息。 |
| `logs` | 查看运行日志。 |
| `logs -f <序号>` | 查看单条日志详情。 |
| `stop` | 停用缓存功能，仅进行透明路由。 |
| `start` | 重新启用缓存功能。 |
| `language chinese` | 切换为中文显示。 |
| `language english` | 切换为英文显示。 |
| `exit` | 优雅停止中间件。 |

日期筛选示例：

```text
list -f 2026-01-01
list -f "2026,01,01-2026,01,31"
list -f 2026-01-01..2026-01-31 -f agent-name
```

## 数据目录

```text
data/
  prefixes/     KV 元数据
  templates/    Agent 前缀参考文件
  sessions/     会话迁移元数据
```

实际 KV 二进制快照由 `llama.cpp` 写入 `slot_save_path`。运行期 `messages` 和 `logs` 只保存在内存中，中间件退出后清除。

以下内容不应提交到 GitHub：

- `config.yml`；
- `data/`；
- slot 快照目录和所有 `*.bin`；
- 模型文件；
- 日志、会话内容和 Agent 模板；
- 本地压缩包、测试输出和虚拟环境。

项目提供的 `.gitignore` 已覆盖这些常见内容，但发布前仍应检查暂存文件。

## 适用条件与限制

- KV 快照通常与模型、`llama.cpp` 构建、上下文参数、KV 数据类型和聊天模板相关；改变这些条件后应重新建立缓存。
- 前缀必须保持一致。Agent 更新系统提示词、工具列表、工具顺序或 Schema 后，会生成新的 KV，而不是错误复用旧 KV。
- 某些 Agent 会额外发送标题生成、摘要或后台任务请求；它们拥有不同前缀，会被识别为独立缓存。
- 当前并发保护针对单个中间件进程。不要让多个中间件进程同时管理同一组 llama.cpp slots。
- 当前版本没有身份认证、TLS、访问控制或多租户隔离。默认仅监听回环地址；不要直接暴露到不可信网络。
- 大型 KV 快照会占用显著磁盘空间，恢复时间取决于磁盘速度和快照大小。
- 该项目不能直接读取 GPU 显存中的 KV；保存和恢复由 `llama.cpp` slot API 完成。

## 故障排查

### KV 一直显示 building

检查 `llama-server` 是否设置了 `--slot-save-path`，配置中的 `slot_save_path` 是否指向同一个目录，以及运行用户是否有写入权限。

### 模型重启后仍重新预填充

依次检查：

1. 快照文件是否存在且大小大于 0；
2. 日志是否出现 `prefix_cache_restored`；
3. `prefix_cache_result.cached_tokens` 是否接近前缀 Token 数；
4. 模型、上下文、聊天模板和 KV 类型是否与建立快照时一致；
5. Agent 的系统提示词或工具列表是否发生变化。

### 切换 Agent 后未命中

使用 `logs -f <序号>` 查看是否出现恢复失败。`--parallel 1` 下中间件会让 KV 请求排队；不要绕过中间件直接向同一个 llama.cpp slot 发送推理请求，否则中间件记录的绑定可能与真实 slot 内容不一致。

### 只想使用透明路由

在管理终端输入：

```text
stop
```

恢复缓存功能：

```text
start
```

## 安全建议

- 默认只在 `127.0.0.1` 上监听；
- 如需局域网或公网访问，请在前置网关增加认证、TLS、限流和访问控制；
- 不要在公开 Issue 中提交完整模板、请求正文、日志或 KV 元数据；
- 发布前执行敏感信息扫描，并人工检查 Git 暂存区；
- 对生产数据执行 `del` 前先备份，删除命令会同时移除磁盘快照。

## 开发

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```

行为变化需要同步更新中英文 README 和技术文档，并通过编译检查与测试。

## 许可证

本项目采用 MIT License，详见 [LICENSE](LICENSE)。

## 发布与贡献

发布前请执行 [发布检查清单](docs/RELEASE_CHECKLIST.md)，并阅读 [贡献指南](CONTRIBUTING.md)。

## 项目状态

当前核心目标是验证“稳定 Agent 前缀的磁盘持久化、slot 切换及模型重启后的恢复收益”。它不是通用会话记忆系统，也不会把旧对话内容注入新会话。

建议在实际工作负载中分别记录：

- 无缓存的首次预填充时间；
- 从磁盘恢复 KV 的耗时；
- 恢复后的新增 Prompt Token 数；
- `cached_tokens`；
- 首 Token 延迟。

这些指标比单独观察 llama.cpp 日志中的进度值更可靠。
