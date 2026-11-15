# 音频处理工具

本目录按功能组织音频处理工具，每个工具的脚本、测试和详细说明放在自己的子目录中。

## 当前工具

| 功能 | 入口 | 使用说明 |
|---|---|---|
| 音频参数检测与规范化：采样率、声道、位深、Peak/LUFS | [normalization/audio_normalizer.py](normalization/audio_normalizer.py) | [normalization/README.md](normalization/README.md) |

```text
audio_processing/
├── README.md
└── normalization/
    ├── audio-norm
    ├── audio_normalizer.py
    ├── configuration.py
    ├── cache_store.py
    ├── configs/
    ├── test_audio_normalizer.py
    ├── test_automation.py
    ├── test_configuration.py
    ├── test_cache_store.py
    ├── README_AGENT.md
    └── README.md
```

## 快速入口

以下命令在项目根目录 `/mnt/d/heliox_2nd` 执行。运行音频检测和处理需要 Python 3.10+、FFmpeg 与 ffprobe；安装方式见工具的详细说明。

查看参数：

```bash
python3 scripts/audio_processing/normalization/audio_normalizer.py --help
```

检测文件或递归检测目录：

```bash
scripts/audio_processing/normalization/audio-norm inspect /path/to/input --json
```

批量转换为16 kHz、单声道、16位 PCM（默认不做音量归一化）：

```bash
scripts/audio_processing/normalization/audio-norm process /path/to/input_directory \
  --output /path/to/normalized_directory \
  --preset audio_16k_mono --json
```

输出和报告应位于输入目录之外；工具拒绝覆盖文件，对内容及配置验证匹配的已完成输出默认复用。单文件处理时，`--output` 应指定 `.wav` 文件路径。

自动化支持JSON/JSONL输出、JSON配置、逐文件内容缓存与续跑、Python SDK。默认缓存位于用户缓存目录，可用 `--cache-dir` 指定。详细调用契约见 [Agent说明](normalization/README_AGENT.md)。

## 目录约定

- `normalization/` 管理当前音频规范化工具及其参数检测功能。
- 将来增加切分、降噪、质量检查时，分别按需建立 `segmentation/`、`denoising/`、`quality_check/`。
- 配置预设放在对应工具的 `configs/` 中。
- 各工具的测试和详细文档与实现放在同一功能目录，总入口在此登记。
- 原始音频、派生音频及运行报告保存在调用者指定的位置，不放进脚本目录。

## 验证规范化工具

```bash
python3 -B -m unittest discover \
  -s scripts/audio_processing/normalization -p 'test_*.py' -v
```

音频测试使用临时合成数据，不修改数据集。工具不在 PATH 时，可通过 `HELIOX_FFMPEG` 和 `HELIOX_FFPROBE` 指定测试所用的可执行文件；缺少工具时，音频集成测试会明确跳过。
