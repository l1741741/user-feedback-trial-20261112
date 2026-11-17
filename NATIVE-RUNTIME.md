本机已验证的运行命令：

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/Users/lichao/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/lib:/opt/homebrew/lib"
.venv/bin/python -m pytest -q cairosvg/test_api.py cairosvg/test_color_flags.py
```

反色开关演示（生成输入素材、四种开关组合的PNG和index.html对照页）：

```bash
.venv/bin/python demo/demo_color_flags.py
```

需要可用的Cairo共享库，其他机器应设置对应本机库路径。
