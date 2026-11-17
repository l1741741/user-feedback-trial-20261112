本目录为补充核验证据，父提交为被评最终版本：7a208afcdcb1242c4c758e8e6688897516fb6bac

以最终源码为PYTHONPATH运行 `python review-evidence/render_check_v2.py gradient-inheritance /tmp/gradient-check`，生成SVG、PNG及三项逐像素结果。focused-baseline为初始版本渲染，focused-final为最终版本；顺序交换分别生成order-a和order-b。

独立检查与模型新增测试分别记录，最终源码未通过本提交补改。
