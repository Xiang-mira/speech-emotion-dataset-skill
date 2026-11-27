# 清单、音频和交付

## 项目输入

`collection.csv` 使用带表头的 UTF-8 CSV。一行对应一个样本，带逗号或换行的文本按标准 CSV 引号规则写入。初次接收其他表格，先按含义映射已有列，保留原文件。

| 字段 | 内容 |
|---|---|
| sample_id、audio | 稳定样本编号，和相对于项目根目录的 WAV 路径。计划文件名可先填写。 |
| scene、group | 业务场景、采录分组。用 group 复算配额，用 scene 核对覆盖。 |
| speaker_id、text | 匿名说话人编号、录制文本或存量录音的实际转写。 |
| intended_label、cue | 朗读预设、单独的表演提示。无朗读预设的存量音频留空。 |
| pair_id | 同一组对照两条共享，普通样本留空。每条仍有独立 sample_id。 |

`config.json` 的 `labels` 为可用标签列表。按项目需要提供 `group_quotas`、`scene_quotas`、按组设置的 `label_quotas`、`pair_groups`、`forbid_label_words` 和 `label_aliases`。听审设置包括 `review_mode`（`joint` 或 `third`）、`show_text` 和 `checkpoint`。

有固定总量时可用 `total_quota` 核验行数。有指定情绪组合时可用 `pair_combinations` 核验每种组合的组数，每组列一次。下例节选自六条家庭采录任务的配置。两个场景各安排一组开心与惊讶对照，因此同一组合列两次。

```json
{
  "total_quota": 6,
  "pair_groups": ["同句对照"],
  "pair_combinations": [["开心", "惊讶"], ["开心", "惊讶"]]
}
```

需要按实际金标验收的分组，用 `gold_quotas` 单独列出各组的目标标签与数量。若每组必须覆盖设计的两类情绪，启用 `require_pair_target_labels`。两条金标互换仍可满足整组覆盖。出现配额缺口时按实际缺口补录或替换，保留原始金标。

## 已定稿的标签

`final.csv` 逐条提供 `sample_id,human_label,initial_disagreement,version,quality`，通过音质验收记为 `pass`。从外部接收定稿时，确认负责人或提供方、定稿版本和质量依据，保留源表或可回查的位置。映射其字段时，只写来源已确认的值。金标不能从朗读预设、模型输出或未裁定的分歧反推。

## 接收端映射

导出前取接收端有效样例或文档，核对字段名、枚举大小写、可空项、编码和路径基准。配置中 `fields` 的键是目标列名，值是来源列名。下例为本工具公开定义的 JSONL 演示协议。

```json
{
  "fields": {"sample_id":"sample_id","audio":"audio","human_label":"human_label","intended_label":"intended_label","pair_id":"pair_id","speaker_id":"speaker_id"},
  "required": ["sample_id","audio","human_label"],
  "omit_empty": true,
  "audio_base": "manifest"
}
```

若接收端使用另一套标签，可加 `label_map` 指定每个中文标签的目标值。`audio_base: "manifest"` 表示音频路径相对输出清单。`omit_empty: true` 省略空的可选字段。接收端若要求最长时长或多声道必须选通道，可配置 `audio_rules`，例如 `{"max_seconds":30,"require_channel_for_multichannel":true}`，并映射 `channel`（从零开始的整数）。时长与通道设置取决于接收端。程序按完整清单检查音频、有效标签及对照关系，生成后回读 JSONL。文件、人工定稿和源清单统一按样本编号关联。缺音频、无标签或配对不完整时先修复或明确重建正式批次，再导出。

只在当前项目的 `.agents/skills/` 安装了技能时，在该项目根目录执行 `python .agents/skills/speech-emotion-eval/scripts/pipeline.py check --project my-first-eval`，将 `my-first-eval` 换成实际清单目录。

下载了完整仓库时，从仓库根目录执行下列命令，将 `PROJECT` 换为当前清单目录。

```text
python skills/speech-emotion-eval/scripts/pipeline.py check --project PROJECT
python skills/speech-emotion-eval/scripts/pipeline.py audio --project PROJECT
python skills/speech-emotion-eval/scripts/pipeline.py export --project PROJECT --final PROJECT/final.csv --profile examples/demo/profile.json --out PROJECT/manifest.jsonl
```

将 JSONL 和按相同相对路径组织的 WAV 一起交给接收端。采录设计、音质检查和听审审计按项目要求另交。第三方平台的兼容结论应来自该平台实际读入结果；使用本工具的演示读入器时在报告里标明读入器名称。
