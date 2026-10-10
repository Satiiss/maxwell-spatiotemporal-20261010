# 本实验包公共计算接口

本目录工具不依赖实验runner，也不调用硬件。公开API变更时同步更新本文。

| 模块 | API | 数据契约 |
|---|---|---|
| `utils/io.py` | `parse_cfg` | 电极、通道、微米坐标字典列表；重复映射必须一致 |
| `utils/io.py` | `read_session`, `select_segment`, `Session` | 原始H5帧原点统一换算；缺少原点或CFG冲突报错 |
| `utils/io.py` | `read_burst_table` | `start_sec,end_sec`，相对基线H5起点 |
| `utils/detection/burst.py` | `detect_burst_intervals`, `merge_intervals` | 复用既有自发检测算法，不改变阈值和回退逻辑 |
| `utils/probe.py` | `count_windows` | 左闭右开窗口，统一population spike计数 |
| `utils/probe.py` | `baseline_distribution` | 同宽背景窗口，剔除任何与burst相交的窗口 |
| `utils/probe.py` | `response_threshold` | 显式指定均值+3SD或均值+3方差 |
| `utils/probe.py` | `evaluate_probe` | 逐trial表、位点汇总、严格>60%可用性 |
| `utils/probe.py` | `rank_units` | 有效电极数优先、其次响应概率，选3unit×2电极 |

此包只实现实验接续所需的Probe计算。完整数据质量分析、稳定延时Map、Consistency和ESRC按用户要求暂缓。

2026-10-10：新增H5/CFG契约和Probe API；复用既有burst模块。
