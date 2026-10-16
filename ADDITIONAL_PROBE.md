# 新增候选unit的独立Probe

普通初次Probe仍要求5–6个中心。补测须显式设置manual_inputs.additional_probe，
引用additional_probe_from原Probe目录。已有候选JSON保留原rank；直接输入中心时自动分配新的unit序号。
补测支持1–10个unit；每unit仍严格为中心及当前CFG物理最近3个记录电极，不自动替换。
接触设备前核对培养物、设备/well、检测阈值、gain、基线路径、CFG哈希及波形/Probe定义。
拒绝使用来源链中任何旧候选电极（包括不可路由的旧位点），或重复unit编号。

用户2026-10-16选择完整10次中至少6次有效。配置明确写probe.min_success_trials: 6。
单trial仍为全部记录电极后[10,50)ms计数严格大于同次前[-50,-10)ms计数的1.5倍。
6/10包含等号；不能只把success_fraction_threshold写成0.60，旧字段是严格大于，缺省仍7/10。
少于完整10次不合格；0–3ms伪迹只做诊断。原始评分和配置不改写。

先生成独立配置（命令在项目根目录执行）：

```bash
python -B tools/prepare_additional_probe.py --source-run <原Probe目录> --review <候选审阅目录> --min-success-trials 6 --output config/<新的独立子目录>
python -B main.py --config-dir config/<新的独立子目录> --headless --probe-only --dry-run
```

这两步不连接设备、不刺激。prepare从原run快照继承培养物、基线和采集设置，使用原run归档CFG，
并核对candidate_units.json中的电极与重新计算的最近邻完全一致；只写本项目config的新子目录。
dry-run应显示4个unit共160次、min_success_trials=6和formal_trial_count=0。
实际Probe必须由操作者另行显式启动hardware模式；完成后停止，不自动正式实验。
正式实验仍须至少3个unit各有至少2个有效电极，并独立人工审阅启动。
本补丁不合并多次run的正式选点，不重新评分旧run。

空间图统一X向右、Y向下，保留CFG原始坐标与电极ID；中心标签#rank: electrode ID。
旧图文件不会自动重绘，显示修复不改变网络计算或刺激选点。

软件检查使用模拟数据；真实Linux安装状态和硬件效果需现场核验。

## 日常补选：只填中心电极编号

不必写JSON或修改代码；同一培养物、同一CFG内使用：

```bash
python -B tools/prepare_additional_probe.py --source-run <最近一次实际Probe目录> --centers <中心ID1> <中心ID2> <中心ID3>
```

每批可选1–10个中心。程序自动匹配物理最近的3个记录邻居，核对中心距离和unit不重叠，
沿来源链排除已计划的旧电极，自动接续最大unit序号（可超过10）。新序号不代表重新计算网络出度排名。
培养物、基线、CFG、检测设置及波形从来源快照继承；显式min_success_trials也继承，
旧来源缺失或null时采用当前补测规则6。若要变更门槛，显式使用--min-success-trials。

输出默认放在config/<新时间戳>_additional_probe，保存完整计划并打印预演命令。
不覆盖已有配置；此前的--review用法继续支持，两种输入不能同时使用。
最近一次实际补测完成后，下次把--source-run更新为该run，让历史检查覆盖全部前序轮次。
不要填dry-run目录。仅检查所选来源链，不自动搜索或合并其他分叉run。
新培养物或CFG改变时须重新提供基线和候选，不能从旧培养物快照准备。
