本目录为补充核验证据，父提交为被评最终版本：3497c1a2aaef58e27481d04e89024c6ec998c601

以最终源码为PYTHONPATH运行 `python review-evidence/visibility_clip_regression.py /tmp/clip-check`，重新生成SVG、PNG、结果及gallery.html。hidden矩形裁剪在Chrome与初始版本中透明，最终版本显示红色；保留首版成功项目的focused-final以区分修复与新增回归。

独立检查与模型新增测试分别记录，最终源码未通过本提交补改。
