本机已验证的运行命令：

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/Users/lichao/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/lib:/opt/homebrew/lib"
.venv/bin/python -m pytest -q cairosvg/test_api.py
```

需要可用的Cairo共享库，其他机器应设置对应本机库路径。
