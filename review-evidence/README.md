本目录为补充核验证据，父提交为被评最终版本：6a549a0691d0893d6f09ab5af6e0293f6a242fb8

以最终源码为PYTHONPATH运行 `python review-evidence/clip_rule_union.py /tmp/clip-union`，生成三组SVG、PNG及六项像素结果、gallery.html。union-final四失败两通过；浏览器实际参照分别确认fill-rule不会替代clip-rule、重复evenodd子区域仍取并集、混合规则保留洞。

独立检查与模型新增测试分别记录，最终源码未通过本提交补改。
