# 按中心列表运行Probe

统一入口为根目录`run_probe.py`，与`run_formal.py`分别手动启动。
在Python文件顶部编辑`CONFIG_DIR`、`CENTER_ELECTRODES`、必要时的`CULTURE_DIV`。
同一培养物和记录配置下，日常只需修改中心列表。无需填写以前的Probe目录、候选JSON或补测/重测模式。

`CONFIG_DIR`指向当前培养物已有的配置目录，提供培养物编号、CFG、600秒Network基线、gain及检测阈值。
即使该目录来自旧的补测准备工具，新入口也会忽略其中的旧模式、旧来源、旧候选编号和旧评审文件。
不会修改原YAML。新培养物必须换成其自己的配置和中心，程序不会从历史结果推导中心。

程序按列表顺序编号`candidate_01...N`；该编号只表示本轮顺序，不从旧轮次猜测编号。
每个中心必须在当前CFG中，使用CFG的物理坐标匹配最近三个记录电极；距离相同时按电极ID排序。
中心至少一个，数量不限制为5–6或10；中心间距和四电极不重叠要求保持。
未知中心、非法ID、中心过近、unit重叠会报错，不自动换点。

固定命令预演，不导入MaxLab、不连接设备：

```bash
python -B run_probe.py
```

输出每个unit的中心、四电极、邻居距离和总次数。新建run目录，保存实际生效的参数快照、CFG副本、
`experiment_plan.json`及`pre_experiment/candidate_sites.csv`、`pre_experiment/candidate_units.png`。
查看候选图时核对星形中心、三个邻居、真实坐标和本轮编号。全部10个中心对应40个位点、计划400次。
提供了基线H5时，预演也会只读核对gain、检测阈值及完整通道—电极—坐标映射。

完成预演后，用另一个固定命令开始真实Probe刺激：

```bash
python -B run_probe.py --enable-hardware
```

每个电极连续10次、2Hz、400mV、每相300µs、先负后正，再切换下一电极，采用single_connect。
后`[10,50)`ms全部记录电极计数严格大于同次前`[-50,-10)`ms计数的1.5倍才成功。
`MIN_SUCCESS_TRIALS=6`为完整10次中至少6次有效；0–3ms伪迹仅用于送达诊断。
配对评分不计算全局非burst背景门槛，不依赖以前的分数。600秒Network仍作为选点来源：
已提供则只读复用，未提供则按配置先记录。每个新run独立评分，保留所有历史结果。

不可路由的位点保存未刺激状态并继续；实际次数可能小于计划次数。Probe结束后停止，不进入正式实验。
旧的`prepare_additional_probe.py`和`prepare_repeat_probe.py`仅保留兼容旧操作记录，不再是推荐路径。
