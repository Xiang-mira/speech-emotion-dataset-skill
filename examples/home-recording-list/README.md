# 家庭语音助手采录清单

[下载录制用 Excel](家庭语音助手采录清单.xlsx?raw=true) · [查看逐条 CSV](collection.csv) · [查看项目配置](config.json)

本例用于观察平静表达是否被误报，以及文字相同时能否区分开心和惊讶。共有六个待录任务。内容为采录设计，WAV 待录，人工金标待独立听审。

| 场景 | 中性样本 | 同人同句对照 |
|---|---|---|
| 灯光状态 | L01，玄关和走廊的灯光状态 | P01，分别以开心、惊讶表达门口的灯又亮了 |
| 物品位置 | K01，遥控器的位置 | P02，分别以开心、惊讶表达照片还夹在书里 |

六条暂分配给同一位匿名录制人 SPK01。录制人只读“录制文本”，按“语气要求”和“表演提示”准备。A/B 组内由同一人逐字朗读相同文字，各录一条 WAV。实际文件按 CSV 的 `audio` 列命名并放置；例如 L01 对应 `audio/L01.wav`。

在仓库根目录运行以下命令可复核采录设计。录音到齐后将本目录和 WAV 一并复制到工作目录，供[听审网页](../../review/README.md)使用。若要立即试用播放和复核流程，可另行启动[听审演示](../demo/coverage.md)。

```sh
python skills/speech-emotion-eval/scripts/pipeline.py check --project examples/home-recording-list
```
