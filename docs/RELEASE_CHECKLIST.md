# Release Checklist / 发布检查清单

## Before publishing / 发布前

- [ ] `python -m compileall -q app.py middleware.py`
- [ ] `pytest -q`
- [ ] `pip install .` or `pip install -r requirements.txt` succeeds in a clean environment
- [ ] `config.yml`, `data/`, `*.bin`, models, logs, archives, and virtual environments are absent from the staged files
- [ ] No private host, port, username, absolute path, credential, full prompt, tool schema, or chat transcript is present
- [ ] README, technical docs, and governance files exist in both Chinese and English
- [ ] The license and repository description match the intended distribution

## Real llama.cpp validation / 真实 llama.cpp 验证

- [ ] `/health`, `/props`, `/slots`, `/apply-template`, and slot save/restore work
- [ ] A first request creates a non-empty snapshot
- [ ] A same-template request reports positive `cached_tokens`
- [ ] Restarting llama.cpp restores the snapshot and reports positive `cached_tokens`
- [ ] Switching templates preserves old snapshots
- [ ] Concurrent requests do not replace an active slot
- [ ] `stop` keeps transparent routing and `start` restores cache behavior

## After publishing / 发布后

- [ ] Configure a private security contact or GitHub Security Advisory
- [ ] Add repository topics and a short description
- [ ] Protect the default branch and require CI checks
- [ ] Create a tagged release with known compatibility notes
