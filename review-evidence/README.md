补充核验证据，父提交为被评最终版本：3497c1a2aaef58e27481d04e89024c6ec998c601

在安装本项目依赖、Pillow和pytest的环境，以最终源码为PYTHONPATH，运行 `python review-evidence/visibility_specific.py 12584 /tmp/render-check` 重新生成SVG、PNG、逐项结果和gallery.html。macOS使用已有Cairo目录设置DYLD_FALLBACK_LIBRARY_PATH。

focused-baseline保留初始版本结果，focused-final保留最终结果；模型回归与独立检查分别记录。
