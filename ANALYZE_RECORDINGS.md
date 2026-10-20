# 独立文件输入的离线分析

入口：`tools/analyze_recordings.py`。不读取`run_probe.py`、`run_formal.py`、培养物编号、
现场YAML或旧run目录；不导入MaxLab SDK，不连接设备。输入是已关闭的MaxWell H5，文件可位于任意目录。
空白输入项跳过，可以只分析一段。代码仍放在实验包中，输入文件的摆放不受此包目录结构约束。

## 填写与运行

在文件顶部的`RECORDINGS`填写路径：

```python
RECORDINGS = [
    dict(name='pre', role='pre', h5='/absolute/path/before.h5', cfg='', trials=''),
    dict(name='stim', role='stim', h5='/other/path/stimulation.h5', cfg='',
         trials='/optional/path/trial_manifest.csv'),
    dict(name='post', role='post', h5='/another/path/after.h5', cfg='', trials=''),
]
CFG_PATH = ''
OUTPUT_ROOT = ''
```

- `name`：英文输出名称，允许字母、数字、下划线和短横线，不要求H5文件名与它相同。
- `role`：`pre`、`stim`、`post`或普通自发`spontaneous`；只有恰好一个pre和一个post才比较前后。
- `h5`：必需；同一H5不能重复充当不同角色。所有相对路径以本项目根目录为基准。
- `cfg`：可选的每文件CFG；空白时使用全局`CFG_PATH`，都为空则使用H5内的记录映射与坐标。
- `trials`：刺激文件可选的标注CSV；空白时从H5事件属性解析条件和双脉冲。
- 每项可附加`well=0`，对应GUI Well 1；默认0。
- `OUTPUT_ROOT`：可选，默认`data/recording_analysis`；每次新增时间戳子目录，不覆盖旧结果。

固定运行：

```bash
conda activate maxlab
python -B tools/analyze_recordings.py
```

实际上任何安装了numpy、h5py、PyYAML、matplotlib的Python 3.10环境均可运行，不要求maxlab包。
输入H5、CFG、CSV只读。结果存到输出目录，不往源H5旁边写文件，也不复制H5到输出目录。

## 可分析的内容

所有文件：完整录制区间的发放率、全部记录电极（含静默电极）的指标、活动栅格图、
群体发放率、burst区间、坐标发放率图。只有显示栅格可抽样，数值计算使用全部有效spikes。
时间原点使用原始frame_nos或经校验的JSON/纯文本起止事件，禁止取首spike或主机估计时刻。
完全缺少明确帧原点的旧spikes-only文件会报错，不能凭首spike补原点。
旧raw文件若有区间外spikes，会单独计数并排除，数据检查提示复核，绝不改写源H5。

刺激文件：从H5内部事件匹配trial，按条件、类型、顺序、延迟和显式电极标注分组，
输出逐trial配对计数、每条件成功比例、PSTH、响应随时间的漂移，以及有电极标注时的延迟曲线。
同步双电极对应一个共同时间事件，双脉冲必须有两个独立时间事件。重复/缺失ID、dt错误、
CSV与H5标签冲突会阻断响应分析，但保留该文件的基础活动结果。

前后比较：要求采样率、gain、spike_threshold已保存且一致，电极/通道/坐标映射一致。
H5有chip_id时也必须一致，well不能不同；不根据路径推断培养物身份，缺失身份元数据仍需人工确认。
按相同记录群体比较发放率、活跃比例、burst频率/持续时间及逐电极变化。
每段使用相同burst参数独立估计阈值，实际阈值写入summary；属于描述性比较。
只有一对录制时不把电极数当作生物重复，不输出培养物层面的p值或因果结论。

本版不重建连接网络，不计算ESRC或单元分类，也不进行依赖raw电压的重检/波形送达判断。

## 参数与计数契约

常用参数直接在入口顶部`PARAMETERS`编辑，每次保存生效参数到输出`config.yaml`。

- 主配对指标：第一发之前`[-50,-10)`ms全部记录电极计数，与最后一发之后`[10,50)`ms计数比较；
  后计数严格大于前计数1.5倍才成功。零背景时至少一个后窗spike。前后窗口必须等宽。
- 这是整对脉冲的响应，不声称分离出第二发独立贡献。正式50次报告实际比例，不套Probe的6/10门控。
- 不按burst剔除主配对窗口；窗口未完整录制或落入其他trial脉冲时不计成功/失败，不用零填充。
- 0–3ms、0–2ms伪迹仅诊断。低于200给出复核提示，不判定“未送达”、不剔除trial。
- 辅助3–200ms统计在最后一发后，比较同宽197ms的非burst背景窗口；背景另排除每发后的200ms。
  少于20个背景窗口时比较不可用，绝不将缺失背景视为零。
- PSTH相对最后一发，覆盖不足或与任何实际脉冲0–3ms伪迹窗相交的bin标为缺失；
  按每个bin实际可用trial数归一化，不把伪迹遮挡画成生物抑制。
- 默认burst使用既有分箱/平滑/阈值算法，但本入口显式关闭density fallback，避免长段持续活动
  因密度回退被全部合成burst。旧选点工具默认行为不变。可在顶部打开该布尔参数。
- 刺激段的总体活动/burst含诱发和伪迹活动，只作定位，不当作自发burst与前后等同解释。
- 不同dt按阶段执行时，延迟差异可能混入时间漂移；应结合response_drift检查，不能只看延迟曲线。

## 标注表与H5单独输入

优先使用现有`trial_manifest.csv`。最小CSV列：

```csv
trial_id,condition,kind,dt_ms,event_ids,electrodes
1,A,single,0,[1],[100]
2,AB,temporal,10,"[2,3]","[100,101]"
```

`kind`支持single、synchronous、temporal；单脉冲/同步各一个event_id，双脉冲两个。
`event_ids`、`electrodes`是JSON整数列表；`electrodes`可为空。
已有表中的onsets_sec、estimated_onsets_sec、send时间等均不用于对齐：只根据event_id读真实H5帧。
可选stage、round、relation原样保留。表没有覆盖全部H5刺激事件时会标记复核。
物理刺激电极来自用户显式标注，不声称由H5记录映射独立证明其送达位置。

没有标注表时，H5事件应带有condition、kind（或type）、pulse、dt_ms属性，支持SDK的JSON或纯文本。
双脉冲的pulse=1/2必须相邻匹配、实际dt正确。此时可以按条件标签分析，但不猜标签对应的物理刺激电极。
没有足够标注则输出response_blocked，保留活动分析，不从文件名、旧候选或原实验配置猜分组。

## 输出与观察重点

```text
OUTPUT_ROOT/<时间>_analysis/
├── config.yaml, source_inputs.json, analysis.log, analysis_result.json
├── data_check.csv / .png / .pdf / .svg
├── pre_post_summary.json, pre_post_electrodes.csv, pre_post_rates.*
└── <name>/
    ├── summary.json, quality.json, electrode_metrics.csv, bursts.csv, population_rate.csv
    ├── activity.*, firing_rate_map.*
    └── trial_responses.csv, condition_summary.csv, psth.csv,
        psth_page_01.*, response_drift.*, temporal_response.*
```

带条件的输出只在有效刺激标注存在时生成；前后对比只在输入满足条件时生成。
先看data_check和analysis_result，随后看activity排查大段沉默/活动漂移；
condition_summary按group_id对应PSTH页中的c001等标题；前后看pre_post_summary和散点图。
图片英文、白底、600dpi，自动保存PNG/PDF/SVG。日志中文，带文件/子任务进度及长步骤心跳。

`complete_with_review`表示计算完成但存在需复核项；`partial`表示至少一个文件读取/处理失败。
每份数据缺少输入、缺标注、无完整响应窗、低伪迹等原因写入CSV的notes。
pre_post_summary.status=blocked时不生成误导性的前后比较图。

## 缓存与内存

OUTPUT_ROOT/.cache按H5/CFG/CSV内容哈希、路径、角色、参数和算法代码建立缓存。
命中时验证缓存输出哈希，复用质检/计数/图形；新分析仍建立独立目录。
输入、参数或代码变动会失效；缓存损坏时重新计算且不覆盖损坏的旧缓存。
哈希以流方式读取文件字节；不加载raw电压数组。H5时间/计数读取使用全部spikes。
读取前给出粗略内存估计（96 bytes/spike），默认预算4GiB，超过则停止该文件，
不应把该估计视为硬性峰值保证。处理长数据前按机器可用内存调整memory_budget_gb。
