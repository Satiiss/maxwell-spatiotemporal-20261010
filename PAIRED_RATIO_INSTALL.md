# 配对评分的Git更新与配置

本规则只用于本项目。更新代码不自动修改实验配置、选择电极或启动刺激。

在已有Linux项目根目录和maxlab环境中先备份当前配置，再更新：

```bash
backup_dir="data/config_backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$backup_dir"
cp -a config "$backup_dir/"
git pull --ff-only
python -B -m unittest discover -s tests -p 'test_probe_paired_ratio.py' -v
```

应显示完整4项测试通过；`Ran 0 tests`不算验证成功。测试均使用模拟/临时数据，不连接MaxLab，不刺激。
如果Git报告本地代码修改冲突，保留工作区并先检查差异，不使用reset或覆盖现场配置。
本规则提交不包含任何YAML、CFG、H5或旧run；此前其他Git版本可能包含配置变更，更新前仍保留备份。

在现场`config/stimulation.yaml`已有probe块中显式设置以下字段（不要重复新建同名probe块）：

```yaml
order: sequential
trials_per_electrode: 10
frequency_hz: 2.0
threshold_rule: paired_pre_stim_ratio
paired_baseline_window_ms: [-50.0, -10.0]
response_window_ms: [10.0, 50.0]
response_to_baseline_ratio: 1.5
success_fraction_threshold: 0.60
enforce_artifact_qc: false
recording_tail_sec: 1.0
formal_unit_count: 3
formal_electrodes_per_unit: 2
```

协议波形由protocol块保持400mV、每相300us、先负后正。本规则比较全部记录电极的群体计数，
不按电极数归一化、不加伪计数、不用全局去burst背景作为成功阈值。后窗必须严格大于1.5倍前窗，等号失败；
前窗为零时后窗必须至少1个spike。缺失完整前窗时拒绝评估。新采集在首发前额外录制100ms覆盖前窗。
每trial保存前计数与独立阈值，汇总表的全局response_threshold为null。

当前GUI没有配对评分选项，使用终端`--headless --probe-only --probe-order sequential`。
真实启动前仍需填写并核对当前培养物、DIV、600秒基线、匹配CFG、当前5–6个中心、检测阈值和gain。
Probe完成后停止，达到至少3个unit各有2个有效电极后再人工审阅正式选点；正式实验独立启动。

如只复核旧Probe数据，使用`tools/review_probe_paired_ratio.py --run <源Probe目录>`。
复核结果另存，不覆盖旧评分，也不能作为`--formal-from`来源。
