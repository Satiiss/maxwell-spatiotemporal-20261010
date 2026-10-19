# 本实验包公共计算接口

本目录工具不依赖实验runner，也不调用硬件。公开API变更时同步更新本文。

`read_session`支持无raw且有`recording_boundary_v1`起止事件的H5。
兼容SDK保存的JSON属性消息（例如`{"recording_boundary_v1":"start","token":"<32位hex>"}`，可有末尾换行）
及旧的纯文本消息；原始eventmessage保留不改写。JSON重复键、无效role/token、保留ID缺少标记均拒绝。
起止标记必须唯一、同token、ID正确且结束帧大于开始帧；以标记帧定义分析区间，
不把首个spike或主机时刻当作原点，也不声称标记等于文件首帧。
`Session.time_origin_source`为`h5_boundary_events`或旧的`raw_frame_numbers`；
spikes/amplitudes排除标记区间外数据并保持逐项对齐，真实刺激事件按同一硬件帧原点换算。
无标记的旧raw文件保持原算法；残缺或矛盾标记拒绝读入，不回退猜测。

| 模块 | API | 数据契约 |
|---|---|---|
| `utils/io.py` | `parse_cfg` | 电极、通道、微米坐标字典列表；重复映射必须一致 |
| `utils/io.py` | `read_session`, `select_segment`, `Session` | 原始H5帧原点或经验证的录制起止事件帧统一换算；time_origin_source区分两者；无原点/CFG冲突报错；amplitudes与times/channels逐项对齐 |
| `utils/io.py` | `read_burst_table` | `start_sec,end_sec`，相对基线H5起点 |
| `utils/detection/burst.py` | `detect_burst_intervals`, `merge_intervals` | 复用既有自发检测算法，不改变阈值和回退逻辑 |
| `utils/spontaneous.py` | `histogram_peak_ratio` | 峰bin计数/非零bin中位数；保留包含峰bin的原定义 |
| `utils/spontaneous.py` | `first_activation_matrix` | channel×burst首次激活ms；缺席为NaN，沿用闭区间burst端点 |
| `utils/spontaneous.py` | `directed_delay_edges` | 共同burst严格>阈值、峰比>=阈值、峰延时绝对值严格>阈值；正B−A为A→B，无边数上限 |
| `utils/spontaneous.py` | `analyze_spontaneous_network` | Session+burst/network参数；完整映射指标、全部边、burst表和首次激活矩阵；可选非重叠人工burst_intervals；以真实时长计算rate |
| `utils/spontaneous.py` | `select_network_candidates` | 确定性排序、中心距离严格>52.5um、3×3排斥、精确最近3邻居、unit不共享电极；只建议，不操作硬件 |
| `utils/network_plots.py` | `render_network_figures` | Session、分析结果、候选、输出路径、plot参数；600dpi PNG/PDF/SVG、100um比例尺、全部边、revision标注；raster仅显示可抽样 |
| `utils/probe.py` | `count_windows` | 左闭右开窗口，统一population spike计数 |
| `utils/probe.py` | `baseline_distribution` | 同宽背景窗口，剔除任何与burst相交的窗口 |
| `utils/probe.py` | `response_threshold` | 显式指定均值+3SD或均值+3方差 |
| `utils/probe.py` | `validate_paired_config` | paired_pre_stim_ratio的前[-50,-10]ms、后[10,50]ms及有限正倍数校验 |
| `utils/probe.py` | `minimum_probe_successes` | 显式min_success_trials为含等号的次数门槛；缺省仍为严格fraction门槛 |
| `utils/probe.py` | `evaluate_probe` | 逐trial表、位点汇总；显式次数门槛要求trial完整；伪迹仅诊断 |
| `utils/probe.py` | `rank_units` | 有效电极数优先、其次响应概率，选3unit×2电极 |

2026-10-14：新增可选`paired_pre_stim_ratio`。比较同次刺激前−50至−10ms与刺激后10至50ms的全部记录电极群体计数，后窗严格超过前窗1.5倍才成功；前窗为零时后窗至少1个spike才通过。不加伪计数、不自动剔除前窗内burst；此规则替代全局去burst背景阈值。前窗缺失则拒绝评估，不当作零。每trial保存前计数、前后rate和独立阈值；summary的全局response_threshold为null，另存trial阈值均值。离线比较入口为`tools/review_probe_paired_ratio.py`，原评分不覆盖，不能作为正式实验来源。

此包实现实验前自发基线的burst/稳定延时出入度Map、候选建议，以及接续所需的Probe计算。完整正式响应数据质量分析、稳定延时Map、Consistency和ESRC按用户要求暂缓。新工具不依赖实验runner；离线入口 `tools/analyze_baseline.py` 负责文件归档和人工复核状态。

2026-10-16：空间图统一使用CFG的左上角原点，X向右、Y向下；只调整绘图轴，不转换电极坐标或ID。此前自发网络图Y向上、Probe unit图Y向下，造成视觉上下镜像。Probe中心标签统一为`#rank: electrode ID`，与候选网络图对应。新增回归检查验证四张网络空间图与Probe unit图的坐标不变、Y轴方向一致。

2026-10-10：新增H5/CFG契约和Probe API；复用既有burst模块。

2026-10-11：补充amplitudes对齐、自发连接、完整出入度与空间候选、离线图形API及参数revision历史；候选须人工复核，不自动开始Probe。

2026-10-11：用户明确响应统计全部记录电极，沿用去burst同宽40ms窗口计数的均值+3方差；0–3ms伪迹只用于排查。Probe与正式实验分开启动，完整有效位点列表保存后等待人工审阅。
