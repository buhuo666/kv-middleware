# Contributing

English | [简体中文](CONTRIBUTING.md)

Thank you for contributing. Before opening an Issue or Pull Request, make sure it contains no model files, KV snapshots, chat content, logs, API keys, or machine-specific absolute paths.

## Development setup

```bash
python -m venv .venv
python -m pip install -r requirements-dev.txt
pytest -q
```

## Pull Request requirements

- Explain the problem, approach, and compatibility impact.
- Add or update tests for behavior changes.
- Run `python -m compileall -q app.py middleware.py` and `pytest -q`.
- Do not commit `config.yml`, `data/`, `*.bin`, models, logs, or local build outputs.
- Keep the Chinese and English README and technical docs synchronized.
- Do not expand scope with authentication, network exposure, or data collection without discussion.

## Code conventions

Prefer clear small functions, the standard library, and existing dependencies. Validate external input and keep file paths within configured directories. Do not guess llama.cpp behavior; rely on actual HTTP responses and tests.

## Issues

Include a minimal reproduction, Python version, llama.cpp build information, relevant redacted management logs, and redacted configuration. Report security issues privately using [SECURITY.en.md](SECURITY.en.md).
