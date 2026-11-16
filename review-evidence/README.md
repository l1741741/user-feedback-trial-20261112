本目录为补充核验证据，父提交为被评最终版本：ed98b803121a4c8b09bf7941c3c888f5e31df867

以最终源码为PYTHONPATH运行 `python review-evidence/paint_order_checks.py /tmp/paint-order-check`，生成相同几何的绘制顺序对照SVG、PNG、七项像素结果和gallery.html。order-baseline三项失败，order-final全部通过。

独立检查与模型新增测试分别记录，最终源码未通过本提交补改。
