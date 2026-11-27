# 语音情绪评测集

从一句业务需求开始，设计场景和配额，生成能直接录制的文本。已有录音可在本机开展双人独立听审，定稿后按接收端要求导出评分清单。

**家庭语音助手采录清单**　[下载 Excel](examples/home-recording-list/家庭语音助手采录清单.xlsx?raw=true) · [查看 CSV](examples/home-recording-list/collection.csv) · [查看配置](examples/home-recording-list/config.json)。灯光状态、物品位置各含一条中性表达及一组开心／惊讶对照，共六条待录，人工金标待听审。[录制说明](examples/home-recording-list/README.md)

## 首次安装与使用

下面以本机 Codex 为例。在项目里安装一次，以后在同一项目中直接描述想测的问题，就能生成采录清单。只有需求、还没开始录音时，也可以从这里开始。

### 1. 打开工作目录

首次使用 Codex，请先安装并登录，在电脑上新建一个文件夹，例如 `voice-eval-work`，然后在 Codex 中将它作为项目打开。[Codex 官方快速入门](https://developers.openai.com/codex/quickstart/)

清单核验脚本使用 Python 3.10 或更高版本，只依赖标准库。Windows 可在终端用 `py -3 --version` 查看版本，macOS 或 Linux 可用 `python3 --version`。如果电脑还没有可用的 Python，可从官网下载。[Python 下载页](https://www.python.org/downloads/)

### 2. 在 Codex 对话中安装

把下面这行复制到刚打开的**项目对话框**并发送。

```text
$skill-installer 请从 https://github.com/Xiang-mira/speech-emotion-eval-kit/tree/main/skills/speech-emotion-eval 安装这个 Skill 到当前项目的 .agents/skills 目录，并确认能找到 .agents/skills/speech-emotion-eval/SKILL.md。
```

安装完成后，在同一项目开一个新对话，输入 `$speech-emotion-eval` 可显式选择技能。如果没有出现，先确认上述路径能打开，再重启 Codex。也可以在 GitHub 主页点击 **Code → Download ZIP**，解压后将 `skills/speech-emotion-eval` 整个文件夹复制到项目的 `.agents/skills/` 下；复制完成后的入口路径相同。[Codex 技能使用说明](https://learn.chatgpt.com/docs/build-skills)

### 3. 一句话生成第一份清单

在同一项目的对话框里直接发送以下需求。以后可替换要测的场景、情绪和数量。

> 请在本项目的 my-first-eval 目录生成家庭语音助手采录清单：灯光状态和物品位置各一条中性，再各做一组同人同句的开心、惊讶对照，写好录制文本和表演提示，并核验配额与配对。

完成后打开 **`my-first-eval/collection.csv`**，用 Excel 或其他表格软件即可查看每条要读的原文、语气要求、表演提示和 WAV 文件名。同目录的 `config.json` 记录标签与配额。这个例子应得到 **6 条任务、4 段不同原文、2 组同人同文对照**。当前是待录清单，人工金标在实际听审后填写。

需要自行复验时，在 `voice-eval-work` 项目根目录运行下面的命令。macOS 或 Linux 将开头的 `py -3` 换为 `python3`。

```sh
py -3 .agents/skills/speech-emotion-eval/scripts/pipeline.py check --project my-first-eval
```

结果中应看到 `ok: true`、`records: 6`、`unique_texts: 4` 和 `pairs: 2`。之后需要试听或组织双人听审时，再下载完整仓库，按下一节启动网页。

## 试用听审网页

下载并解压完整仓库，在根目录运行以下命令。需要 Python 3.10 或更高版本。Windows 也可双击 `Start-Demo.cmd`，或将命令中的 `python` 换成 `py -3`。

```sh
python run.py demo
```

首次运行会创建 `work/demo/` 及其清单、配置，再使用[听审演示文本](examples/demo/collection.csv)在本机准备六段 WAV。Windows 优先使用 PowerShell 和 System.Speech 可访问的中文语音。也可使用带普通话语音的 eSpeak NG，Linux 和 macOS 采用这一路径。若首次启动提示缺少合成引擎，在已创建的工作目录中按清单的文件名将六段自备 WAV 放入 `work/demo/audio/`，再运行同一命令。首页的待录清单与听审演示使用不同文本，各自配套使用。

1. 终端会显示两个听审人专属链接。分别用两个独立浏览器会话打开，逐段在页面中播放、判断、提交。听审进度保存在 `work/demo/`。
2. 两人均提交整轮后，一致且音质合格的样本得到人工金标。有分歧时，一方复听后提出带理由的结论，另一方确认同一版本。第三人裁定演示使用 `python run.py demo --mode third --port 8766`，数据写入 `work/demo-third/`。
3. 从网页下载定稿清单和审计记录。终端按 Ctrl+C 结束服务，再次启动同一模式即可继续。

## 使用自己的录音和定稿

| 现有材料 | 操作 | 交付结果 |
|---|---|---|
| 业务需求、候选句或旧表 | 用技能生成场景、录制文本及配额，逐组核验 | 覆盖设计、采录清单、配置和核验结果 |
| 录音和编号清单 | 准备 `collection.csv`、`config.json` 和相对路径对应的 WAV，运行 `python run.py review --project 项目目录` | 初审原判、分歧、复核结论及审计记录 |
| 人工定稿和对应录音 | 确认版本与质量依据，配置字段和标签映射，导出 JSONL | 评分清单、配套 WAV 和回读核验结果 |

以下命令以双人演示为例。将网页下载的定稿清单保存为 `work/demo/final.csv` 后运行：

```sh
python skills/speech-emotion-eval/scripts/pipeline.py check --project work/demo
python skills/speech-emotion-eval/scripts/pipeline.py audio --project work/demo
python skills/speech-emotion-eval/scripts/pipeline.py export --project work/demo --final work/demo/final.csv --profile examples/demo/profile.json --out work/demo/manifest.jsonl
```

接收端字段、标签和音频要求可按实际协议设置。[七类 WAV 映射示例](examples/profiles/seven-label-wav.json)要求单条不超过 30 秒，并对多声道文件指定语音通道。通用采录阶段的时长以本轮需求为准。

## 方法与验证

- [场景、配额与文本提示词](skills/speech-emotion-eval/references/design.md)
- [听审和分歧处理](skills/speech-emotion-eval/references/review.md) · [听审网页操作](review/README.md)
- [字段与评分清单](skills/speech-emotion-eval/references/delivery.md)
- [行为测试用例](evals/cases.md) · [验收结果与模拟导出](evals/results.md)

本地运行 `python -m unittest discover -s tests -v` 可复验程序行为。模型表现需要在实际录音和经确认的人工金标上另外评测。
