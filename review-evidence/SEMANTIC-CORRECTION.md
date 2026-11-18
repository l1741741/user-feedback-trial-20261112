# 9339 遮罩百分比检查的更正

被评最终提交：bd1effef08f66371ef9a71dbaafc123914cef38c。

原additional-render的六项探针保留5项通过、1项失败，录像中的退出码1保持原义。percentage-right的断言把mask内容中的width="50%"直接当作包围盒数值0.5处理，预设右半必须透明。SVG 1.1第7.11节区分mask区域属性与mask内容的百分比；内容按Units处理，不能由区域属性的等价规则推出这个断言。

录像里fraction场景的浏览器SVG和PNG均为半宽；percentage场景的浏览器SVG和PNG均为完整红块。两者不相等本身不足以认定最终实现有错。原探针的这项失败是检查预期错误；结果文件、录像、脚本以及原评分和Feedback均另保留，不将失败行改写为通过。

重新审阅cairosvg/mask.py、surface.py及cairosvg/test_mask.py：_to_alpha_mask区分亮度乘alpha和直接alpha，paint_mask按区域裁切并在合成后施加对象透明度，浅复制遮罩定义避免共享节点改写。19项回归与3项基础独立图像检查原始执行均通过；它们与录像的6项探针是不同运行，不替换数字。

修订评价为5分，依据已验证的亮度/alpha区分、组透明度一次相乘、区域裁切、对象坐标复用及重复导出；不将原探针失败认作实现缺陷。MASKING.md列出的SVG2长属性、多层遮罩及部分包围盒回退仍是明确的范围限制。

规范来源：https://www.w3.org/TR/SVG11/coords.html#ObjectBoundingBoxUnits 和 https://www.w3.org/TR/SVG11/coords.html#Units 。
