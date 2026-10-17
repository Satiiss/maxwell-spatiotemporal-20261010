# 全部原候选重新Probe

普通初次Probe仍接受5–6个中心；新增补测仍拒绝旧位点。明确要重测全部候选时，使用
独立的repeat_probe配置，允许重新测试来源链中的原候选，最多10个unit。

在项目根目录打开tools/prepare_repeat_probe.py，只改顶部SOURCE_RUN、OUTPUT_DIR、CULTURE_DIV。
SOURCE_RUN填写本培养物最近一次已收尾的Probe目录；程序沿additional/repeat来源链读取candidate_sites.csv，
恢复全部原中心、四电极集合和candidate编号，不读取或复用旧评分。重复编号必须对应完全相同的原候选。
只允许同一培养物、CFG、基线、采集参数和协议；不允许借此把旧培养物选点套到新培养物。

OUTPUT_DIR留空时自动生成时间戳配置目录；指定目录时必须是本项目config下的新子目录，不覆盖已有目录。
实验日期更新为准备当天，DIV可在文件内填写本次实际值；MIN_SUCCESS_TRIALS默认6。

```bash
python -B tools/prepare_repeat_probe.py
```

该命令不连接设备，不刺激；只生成system/stimulation快照和repeat_probe_plan.json。
先核对打印的全部unit、四电极和总次数，再运行程序打印的main.py预演命令。
10个unit应为40个候选、计划400次。手动去掉dry-run并显式加hardware开关后才会真实刺激。

逐位点连续10次、2Hz、400mV、每相300us、先负后正；同次后[10,50)ms群体计数严格大于
前[-50,-10)ms的1.5倍才为成功，完整10次中至少6次有效。0–3ms伪迹仅诊断。
复用同一已核对的600秒Network基线及CFG，新建独立run、新采集H5和新评分，原记录保留。
曾经不可路由的原候选仍列入重测；若本轮仍不可路由，保存未刺激状态，继续其余原候选。
因此400为计划次数；实际完成次数以新run记录为准。完成后停止，不自动进入正式实验。

启动前仍核对已有基线的检测阈值/gain及H5/CFG映射。repeat模式不能同时开启additional模式，
也不能删除来源清单中的部分unit或改变最近邻后仍声称整轮原位重测。

```bash
python -B -m unittest discover -s tests -p 'test_repeat_probe.py' -v
```

应有3项通过；仅模拟数据，不连接MaxLab或发送真实刺激。
