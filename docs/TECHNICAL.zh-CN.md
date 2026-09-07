# 技术文档

[English](TECHNICAL.en.md) | 简体中文

## 1. 目标与边界

本项目把 Agent 请求开头稳定的 `system`/`developer` 消息和 `tools` 作为“可复用前缀”。用户消息、助手回答、工具返回和会话历史不进入通用前缀快照。

中间件不读取 GPU 显存，也不实现模型推理。它通过 llama.cpp 的 HTTP slot API 完成预填充、保存、恢复和透明转发。

## 2. 请求路径

```text
客户端 -> middleware.py/uvicorn -> app.py
       -> 前缀识别与 Hash
       -> /apply-template
       -> /completion (n_predict=0)
       -> /slots/{id}?action=save 或 restore
       -> /v1/chat/completions
       -> 客户端
```

`middleware.py` 负责启动 uvicorn 和交互式命令行。`app.py` 负责配置、HTTP API、缓存生命周期和上游代理。

## 3. 前缀识别

`agent_prefix(payload)` 从 `messages` 开始扫描：连续的 `system` 和 `developer` 消息进入前缀，遇到第一个其他角色后停止；`tools` 始终属于前缀。动态工作区行会在 Hash 和转发前清理。

规范化后的对象使用稳定排序和紧凑 JSON 编码，再计算 SHA-256。相同模板得到相同 `prefix_hash`；工具顺序、Schema 或提示词变化都会得到不同 Hash。

## 4. 快照建立

新模板第一次到来时，中间件：

1. 查询 `/props` 获取模型指纹和 slot 数量；
2. 分配空闲 slot，必要时淘汰空闲绑定；
3. 调用 `/apply-template` 得到 llama.cpp 实际渲染的前缀；
4. 对前缀调用 `/completion`，`n_predict=0`；
5. 调用 slot save；
6. 只有 `n_saved == token_count` 且快照文件存在且非空时才标记 `ready`。

快照元数据位于 `data/prefixes/<id>.json`，模板参考位于 `data/templates/<id>.template.json`，二进制由 llama.cpp 写入 `slot_save_path`。

## 5. 恢复与命中判断

恢复前会检查：

- 快照状态为 `ready`；
- `snapshot_scope` 为 `prefix_only_v2`；
- 保存 Token 数等于前缀 Token 数；
- 文件路径位于配置的 `slot_save_path` 内；
- 请求的前缀 Hash 与元数据一致。

恢复接口返回的 `n_restored` 用于确认恢复规模；最终命中必须以模型响应的缓存统计为准。中间件优先读取 `usage.prompt_tokens_details.cached_tokens`，其次读取兼容的 OpenAI `usage` 字段，再读取 llama.cpp 常见的 `timings.cache_n`/`n_cache` 字段。如果响应没有任何缓存计数，则显示未知，不根据 restore 请求自行猜测命中。

## 6. Slot 轮换

单 slot 部署时，新的模板按以下顺序选择目标：

1. 上游 `/slots` 报告的空闲且没有元数据的 slot；
2. 空闲且已有绑定的 slot，按最近使用时间淘汰。

只清除进程内 `loaded_slot_prefixes` 绑定，不删除旧元数据、模板和二进制快照。`is_processing=true` 的 slot 永不淘汰。

## 7. 并发模型

KV 请求使用进程内 `asyncio.Lock` 覆盖完整生命周期：恢复、转发、读取普通响应或消费完整 SSE 流。这样 `--parallel 1` 时一个请求不会在模型处理过程中被另一个模板替换。

这把锁不能跨进程工作。因此同一组 llama.cpp slots 只能由一个中间件进程管理；也不能绕过中间件直接向这些 slot 发请求。

## 8. 配置安全

`load_config()` 启动时读取 YAML，并检查：

- 根节点和各 section 必须是 mapping；
- 上游地址必须是绝对 `http`/`https` URL；
- 超时必须为正数；
- Agent 端口必须在 `1..65535`；
- 存储目录不能为空。

快照文件名只允许解析到配置的快照目录，防止路径穿越。默认监听回环地址，不提供认证、TLS 或多租户隔离。

## 9. 故障与恢复

- llama.cpp 不可达：请求返回 502，服务本身仍可运行；
- slot save/restore 不支持：内存模式或明确错误，不伪造 `ready`；
- 快照缺失：记录错误并重新建立；
- 元数据损坏：返回 500 并指出文件名；
- 正在处理的 slot：返回 409 或等待其他请求释放锁。

## 10. 测试策略

本项目的 `tests/test_core.py` 覆盖前缀边界、路径限制、删除清理、迁移恢复和配置校验。CI 在 Python 3.10 和 3.12 上执行编译检查与测试。真实模型兼容性仍需在目标 llama.cpp 构建上验证。
