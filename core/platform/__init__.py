"""Phase 2 平台层（骨架）：平台无关消息组件 + Platform 抽象 + OneBot V11 adapter。

对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.4。目标是让内核与 IM 平台解耦：
内核只见 MessageChain 组件，OneBot 段转换封闭在 adapter 两侧。
"""
