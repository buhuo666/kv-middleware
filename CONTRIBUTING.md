# 贡献指南

[English](CONTRIBUTING.en.md) | 简体中文

感谢贡献。提交 Issue 或 Pull Request 前，请先确认没有包含真实模型文件、KV 快照、聊天内容、日志、API 密钥或本机绝对路径。

## 开发环境

```bash
python -m venv .venv
python -m pip install -r requirements-dev.txt
pytest -q
```

## Pull Request 要求

- 说明问题、方案和兼容性影响；
- 为行为变化增加或更新测试；
- 运行 `python -m compileall -q app.py middleware.py` 和 `pytest -q`；
- 不提交 `config.yml`、`data/`、`*.bin`、模型、日志或本地构建产物；
- 保持 README 和技术文档的中英文同步；
- 不在未讨论的 Pull Request 中引入认证、网络暴露或数据收集等扩大范围的功能。

## 代码约定

使用清晰的小函数，优先标准库和现有依赖。外部输入必须校验，文件路径必须限制在配置目录内。对 llama.cpp 的行为不要猜测，使用实际 HTTP 响应和测试说明。

## Issue

Bug 报告请提供最小复现、Python 版本、llama.cpp 构建信息、相关管理日志和脱敏配置。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。
