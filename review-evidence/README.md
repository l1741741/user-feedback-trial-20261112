补充核验证据，父提交为被评最终版本：21f335b30fc7af11cd819cd4a5ec8719d1ce7bc9

在安装本项目依赖、Pillow和pytest的环境，以最终源码为PYTHONPATH，运行 `python review-evidence/mask_bbox_percentage.py /tmp/render-check` 重新生成SVG、PNG、逐项结果和gallery.html。macOS使用已有Cairo目录设置DYLD_FALLBACK_LIBRARY_PATH。

bbox-baseline保留初始版本结果，bbox-final保留最终结果；模型回归与独立检查分别记录。
