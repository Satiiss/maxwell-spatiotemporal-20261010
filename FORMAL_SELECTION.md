# 正式采集与Probe解耦

正式采集读取独立选点，而不要求某个Probe run整体合格。Probe继续生成自己的评分、候选报告和
审阅状态；这些结果不再控制独立正式入口能否运行。旧`--formal-from`作为严格单run兼容入口保留。
正式刺激条件、波形生成、快切、事件时间及三段录制算法不变，完整分析pipeline/ESRC仍暂缓。

## 正式记录的存储方式

在`run_formal.py`已有的`SELECTION`字典中增加`"recording_mode": "spikes_events",`，
正式三段H5仅保存全部记录电极的spikes、刺激/边界事件、映射和SDK元数据，不定义raw电压group。
缺省保持原raw模式；`"recording_mode": "raw"`显式保存全部记录通道电压。
无需改源Probe快照或本机YAML，也不改变三段流程、5400 trial、波形、条件或Sequence等待。
实际模式保存到新run的配置、计划、选点记录和block元数据。Probe继续要求raw。

无raw时不能依靠`groups/*/frame_nos`：每段开始和结束分别发送只含标注的Event，
使用保留ID 1048574/1048575及唯一token。每个标记通过只含初始延迟和Event的Sequence发送，
不含DAC或刺激单元命令；用官方Sequence.append()（返回Sequence本身），检查sequence_send返回值，
Sequence对象保留至录制文件关闭。底层sequence_append在现场可返回空字符串，不能强求OK；
最终以H5内实际记录的起止标记判定成功，不以append返回对象作为送达证据。
SDK会将Event属性转为JSON保存；读取器同时识别JSON与旧纯文本消息，校验规则相同。
不再直接mx.send(Event)：现场SDK该调用返回空字符串。也不把空返回当作成功。
读回H5验证唯一、同token、先后正确的标记，以其硬件帧差定义分析区间，
`time_origin_source=h5_boundary_events`，明确不声称起点是文件首帧；排除起点前和结束后的spikes。
起点不取首个spike、不取主机时间；刺激时间仍取本段H5的实际刺激事件。
结束标记后保持writer活动1秒再停录，之后另等SDK写盘时间；异常也执行停录和关文件。
每段保存`recording_interval.json`，含来源、帧原点、采样率及区间长度。
前600秒自发的起止标记与无raw检查通过后，才允许进入刺激；缺失/重复标记或意外raw组会停止。

现场首次使用或SDK兼容问题修复后，可先运行`python -B tools/check_formal_recording.py --enable-hardware`。
它只读取当前`run_formal.py`的CONFIG_DIR，在当前硬件采集状态下另录三秒spikes/events并读回核对，
不initialize、不download、不offset、不修改阈值/gain、不发送DAC或刺激单元命令，也不进入正式实验。
默认不加硬件标志只显示配置。检查通过只证明短录制的映射、无raw和实际H5时间标记，
采集设置另行打印供核对；真实正式启动仍会按原流程初始化和录制600秒。
已有短录制可通过`python -B tools/check_formal_recording.py --h5 /实际路径/storage_check.raw.h5`
只读复核；不连接SDK、不重新录制、不修改原H5或结果文件，也不会进入正式实验。

这种模式不能回看完整电压、重新检测spikes或进行依赖原始波形的排查/排序。
文件大小取决于实际spike数，不能把raw的0字节说成整场数据为0字节；计划不虚构固定大小。
未压缩全通道raw参考量仍另存供比较。正式时长的名义估计不含SDK及writer收尾开销。
SDK真实无raw文件的首段读回仍须现场核验；软件模拟通过不代表现场验收完成。

官方依据：[Saving教程](https://api-docs.mxwbio.com/tutorial_index.html)，
[Event API](https://api-docs.mxwbio.com/section_api/subsections/api_python.html#maxlab.system.Event)。

## 编辑文件，使用固定命令

在项目根目录打开`run_formal.py`，编辑顶部`CONFIG_DIR`、`INTER_SEQUENCE_REST_SEC`和`SELECTION`：

- `CONFIG_DIR`：本培养物当前的配置目录，确认其中culture、DIV、日期、CFG、采集设置和formal参数。
- `culture_id`：须与当前配置的培养物一致，默认空值会拒绝运行。
- `units`：恰好三项，每项两个电极ID。列表顺序映射为正式unit 1/2/3；原unit编号仍单独留档。
- `minimum_success_trials`：选点参考门槛，默认6/10，不修改源Probe定义或原评分。
- `INTER_SEQUENCE_REST_SEC`：每条Sequence最后一个完整双相脉冲结束后的静息秒数。
  `None`沿用当前配置的`formal.trial_interval_sec`（仓库默认1秒）；例如填`3.0`表示静息3秒。
  正式入口固定使用after_second口径，单电极/同步条件也从完整脉冲结束后计时；静息至少0.05秒以保留响应窗。

真实执行顺序为：发送Sequence → 保持原连接及Sequence对象，等待全部脉冲结束并静息 →
切换到下一组电极并等待settling → 发送下一条Sequence。等待从发送接口返回后保守计算，
计入Sequence初始延迟，兼容提前确认和阻塞返回；不再在等待下一trial之前切换电极。
静息期间持续录制，最后脉冲后的50ms响应窗包含在静息期间。Probe仍按原2Hz执行。

下列仅为格式示例，电极和路径均须自行填写，不代表推荐选点：

```python
CONFIG_DIR = "config/YOUR_CURRENT_CONFIG"
SELECTION = {
    "culture_id": "YOUR_CULTURE",
    "minimum_success_trials": 6,
    "units": [
        {"source_run": "data/PROBE_RUN_A", "source_unit": "candidate_01",
         "electrodes": [100, 101], "exceptions": {}},
        {"source_run": "data/PROBE_RUN_B", "source_unit": "candidate_07",
         "electrodes": [200, 201], "exceptions": {}},
        {"source_run": "data/PROBE_RUN_B", "source_unit": "candidate_08",
         "electrodes": [300, 301], "exceptions": {301: "人工纳入该位点的具体理由"}},
    ],
}
```

路径相对于选点文件所在目录。source_unit须使用源candidate_sites.csv中的完整名称。
不会写死任何实际培养物或Unit 3/7/8，也不会自动填入旧培养物电极。

固定预演命令：

```bash
python -B run_formal.py
```

默认仅预演，不导入MaxLab、不连接设备。核对输出的六个电极、实际条件表、次数、名义时长和配置快照。
仓库默认正式参数为5400 trial、名义约181.9分钟，含正式前/后各600秒自发、阶段休息、
每条Sequence初始延迟5ms及每次切换50ms settling；不含通信、序列构建等额外耗时。
若静息从1秒改为3秒，5400个trial合计增加180分钟，名义约361.9分钟。
默认1024通道20kHz uint16未压缩约447GB，实际文件大小与压缩及记录通道数有关。

单独检查六电极联合路由（会连接设备、下载路由和校准，但不创建Sequence、不发刺激、不录H5）：

```bash
python -B run_formal.py --routing-check --enable-hardware
```

正式采集必须另行显式执行；下条命令会发送真实刺激：

```bash
python -B run_formal.py --enable-hardware
```

完成路由检查不会自动接续正式采集；实际正式采集开始时也会重新验证六电极路由。
要求6个记录电极互不重复，属于当前CFG，分别路由到不同刺激单元，且路由不改变记录映射。

## Probe只是可选的选点证据

提供source_run时，来源可以是probe_complete或probe_review_required，支持跨多个run。
校验培养物、CFG、设备/well、检测阈值/gain及波形一致；检查电极确实属于所注明的源unit。
读取的是已关闭run的CSV/JSON/快照，不重读H5、不重新评分、不修改旧run。

以真实成功次数对照本次参考门槛，原eligible字段另存。例如旧7/10政策下被标为False的6/10，
在本次6/10参考门槛下可以选用，但源False仍保留。低于本次参考门槛时，逐电极填写exceptions理由；
5/10仍记录为5/10及meets_reference_policy=false，不伪装成6/10。例外不能伪造缺失、未测或
不可路由的Probe记录；声称引用Probe证据时须有完整10次且成功次数与比例一致。

若完全由人工指定、不引用Probe，可以省略source_run，提供name、electrodes和selection_reason。
这种选点明确保存为manual_unscored，各Probe分数字段为null，不标成Probe合格。
不再要求manual_inputs、候选中心、旧基线或stimulation.probe配置参与正式执行。

自动化程序也可生成同结构的JSON，使用：

```bash
python -B main.py --headless --formal-selection /path/to/selection.json --dry-run
```

独立选点文件是人为编辑的Python或JSON；Python文件会执行其中定义，应只使用自己的文件。

## 留档与验证

预演和正式run均保存formal_selection.json（实际选点、真实分数、人工理由、来源文件SHA256）、
formal_selection_source.py/json（输入文件副本）、formal_condition_plan.csv（实际六电极的108条件表）
和experiment_plan.json。正式采集另保存三段H5、事件时间及trial日志；不生成新的Probe段。
零刺激检查保存formal_routing_check.json；正式采集的参数仍由本次config_snapshot记录。

```bash
python -B -m unittest discover -s tests -p 'test_formal_selection.py' -v
```

应为9项通过。软件检查只使用模拟，不代表现场六电极已完成联合路由验收。
