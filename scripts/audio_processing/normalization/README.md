# 音频检测与规范化

入口：[audio_normalizer.py](audio_normalizer.py)。需要 Python 3.10+、FFmpeg 和 ffprobe；Python 端仅使用标准库，无需安装 Torch 或其他 pip 依赖。

返回 [音频处理工具总览](../README.md)。以下命令均从项目根目录 `/mnt/d/heliox_2nd` 执行；脚本自身帮助中的短命令示例适用于脚本所在目录。

自动化接口详见 [README_AGENT.md](README_AGENT.md)。SDK与CLI共享配置、缓存、路径保护和处理逻辑；当前版本为2.0。

## 推荐的自动化调用

```bash
# 仅元数据检测：不运行音频解码和响度分析
scripts/audio_processing/normalization/audio-norm inspect /path/to/input \
  --metadata-only --json

# 格式预设：16 kHz、单声道、16位PCM，不施加响度增益
scripts/audio_processing/normalization/audio-norm process /path/to/input \
  --output /path/to/output --preset audio_16k_mono --json

# 显式选用-23 LUFS预设，逐文件输出JSONL
scripts/audio_processing/normalization/audio-norm process /path/to/input \
  --output /path/to/output_lufs --preset audio_16k_mono_lufs23 --jsonl
```

快捷入口按自身位置定位脚本，可从任意工作目录通过绝对路径调用。`AUDIO_NORM_PYTHON` 可指定Python解释器；不修改用户Shell配置。`--preset`根据工具目录定位内置JSON，与当前工作目录无关。

### 结构化输出

- `--json`：stdout仅一个JSON对象，单文件和批量统一使用 `files` 数组。
- `--jsonl`：每个文件完成即输出并flush一个 `type="file"` 记录，最后输出 `type="summary"`；摘要的 `files=[]`，磁盘报告仍保存完整数组。
- 机器模式不向stdout混入日志；不要将stderr合并进JSON解析流。
- 输出 `schema_version="1.0"`；全局和逐文件错误都有稳定 `error_code`，未知值也应作为失败处理。
- 文件 `status` 为 `ok/skipped/error`；缓存命中为 `skipped`、`reason="cache_hit"`。
- 汇总 `status` 为 `success/partial_failure/error`；`succeeded`包含复用，`processed`只计本次新执行。
- `normalization_target_met`单独说明归一化目标是否达到；静音跳过不应被当成达到目标。

### 配置预设

提供 [仅格式预设](configs/audio_16k_mono.json) 和 [格式加LUFS预设](configs/audio_16k_mono_lufs23.json)。自定义配置通过 `--config /path/to/config.json` 加载，与 `--preset`互斥。

配置优先级为 **显式CLI/SDK参数 > JSON文件 > 内置默认值**。字段名与CLI对应，如 `target_sample_rate`；`bit_depth`是整数16/24/32。未知字段、重复键、非有限数、错误类型和范围均拒绝。当前只支持JSON，不需要YAML依赖。

报告包含最终生效配置与配置哈希。−23 LUFS是可选方案，应根据具体TTS模型要求选择，不视为所有模型的强制规范。

### 缓存与续跑

默认开启内容缓存与验证续跑。重复同一请求时，不重复完整解码和转换已验证的结果；仍读取内容以核对SHA-256。

缓存路径优先级：`--cache-dir` / SDK参数 → `AUDIO_NORM_CACHE_DIR` → `$XDG_CACHE_HOME/audio-normalizer` → `~/.cache/audio-normalizer`。受限环境建议明确指定输入目录之外的可写缓存路径。

```bash
scripts/audio_processing/normalization/audio-norm process /path/to/input \
  --output /path/to/output --preset audio_16k_mono \
  --cache-dir /path/to/audio_cache --json
```

- 缓存身份包含输入内容、测量阶段、有效信号参数、代码及工具版本/构建身份。原始测量、转换后测量和元数据检测分别缓存。
- 输出存在时，还必须核对输出内容哈希；输入、配置或输出变化均不能静默跳过或覆写。
- 每个文件独立提交SQLite记录；发布输出前保存prepared凭据，中断后可核对已发布输出再恢复。
- 缺失输出会重新生成；未匹配的已有输出报冲突，需要另选输出位置。
- `--no-resume`禁用已有输出复用，测量缓存仍可用；`--no-cache`禁用缓存读写，也无法复用已有输出。
- 元数据模式省去解码，但默认内容缓存仍读取文件计算哈希，不是常数时间的文件头检查。
- 缓存使用POSIX文件锁，支持多个进程共享；一个SDK实例应在一个调用线程内使用。当前面向Linux/WSL/macOS，不支持原生Windows Python。
- 缓存和锁是运行产物，不自动清理；删除缓存将失去对应续跑凭据。

### Python SDK

项目根目录需位于Python导入路径。直接调用不打印、不退出宿主程序：

```python
from scripts.audio_processing.normalization.audio_normalizer import AudioNormalizer

with AudioNormalizer(
    target_sample_rate=16000,
    target_channels=1,
    normalize="none",
    cache_dir="/path/to/audio_cache",
) as normalizer:
    metadata = normalizer.inspect("/path/to/input.wav", metadata_only=True)
    result = normalizer.process_file("/path/to/input.wav", "/path/to/output.wav")
    summary = normalizer.run(
        "/path/to/input_directory",
        command="process",
        output="/path/to/output_directory",
    )
```

构造器也接受 `config="/path/to/config.json"`。单文件方法返回逐文件记录；`run()`返回汇总，并收集逐文件错误。全局错误和单文件方法失败会抛出带`.code`的异常或文件系统异常，由宿主处理。`process_file()`不单独写汇总报告，`run(command="process")`默认写报告。

## 安装依赖

Ubuntu / Debian / WSL：

```bash
sudo apt-get update
sudo apt-get install ffmpeg
ffmpeg -version
ffprobe -version
```

macOS 可用 `brew install ffmpeg`；Conda 可用 `conda install -c conda-forge ffmpeg`。也可以在命令末尾指定 `--ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe`。

## 检测文件或目录

```bash
python3 scripts/audio_processing/normalization/audio_normalizer.py inspect /path/to/audio.wav

python3 scripts/audio_processing/normalization/audio_normalizer.py inspect /path/to/input_directory \
  --report /path/to/inspection.json
```

目录会递归扫描音频文件。检测会完整解码，输出采样率、声道数、位深、时长、sample peak、RMS、integrated LUFS；JSON 中还包含 dBFS、true peak、响度范围、编码信息和逐文件错误。

测量范围为第一个音轨，报告明确记录音轨数量。处理命令拒绝多音轨或超过两声道的输入，避免猜测映射。

## 格式转换

单文件输入必须指定一个新的 WAV 文件名：

```bash
python3 scripts/audio_processing/normalization/audio_normalizer.py process /path/to/input.wav \
  --output /path/to/output.wav \
  --target-sample-rate 16000 --target-channels 1 --bit-depth 16
```

目录输入需要指定独立的输出根目录，输出保留相对目录结构：

```bash
python3 scripts/audio_processing/normalization/audio_normalizer.py process /path/to/input_directory \
  --output /path/to/normalized_directory \
  --target-sample-rate 24000 --target-channels 1 --bit-depth 24
```

目标采样率应按 TTS 模型的要求选择。16 kHz 升采样到 24/48/96 kHz 仅适配输入格式，不能恢复已丢失的高频。

默认使用 FFmpeg SWR 的64点滤波配置。`--resampler soxr` 使用 SoXR、precision=28，需要 FFmpeg 构建包含 libsoxr；不支持时明确报错，不静默切换算法。

单声道转双声道：

```bash
python3 scripts/audio_processing/normalization/audio_normalizer.py process /path/to/mono.wav \
  --output /path/to/stereo.wav \
  --target-sample-rate 48000 --target-channels 2
```

左右声道复制同一信号，不产生新的空间信息。双声道转单声道默认 `0.5L + 0.5R`；也可指定 `--mono-source left` 或 `--mono-source right`。完全抵消或选中静音通道会报错；部分抵消仍需结合实际声道质量判断。

## 可选音量归一化

默认 `--normalize none`，不施加归一化增益。重采样及编码本身仍可能改变峰值或产生量化差异。

峰值归一化到 −1 dBFS：

```bash
python3 scripts/audio_processing/normalization/audio_normalizer.py process /path/to/input.wav \
  --output /path/to/peak_normalized.wav \
  --normalize peak --peak-dbfs -1
```

EBU R128 双遍归一化到 −23 LUFS：

```bash
python3 scripts/audio_processing/normalization/audio_normalizer.py process /path/to/input.wav \
  --output /path/to/lufs_normalized.wav \
  --normalize lufs --target-lufs -23 --true-peak-dbfs -1 --lra 7
```

首遍先执行目标声道映射及重采样，再测量响度；第二遍复用相同处理链和测量参数。FFmpeg 可能因 true peak/LRA 约束从线性模式切换为动态模式，实际模式记录在报告中。最终强制目标采样率，并重新检测输出。

静音或很短的片段可能没有有效的 integrated LUFS。默认拒绝对其做 LUFS 归一化；可显式添加 `--unmeasurable-lufs skip-gain`，只转换格式并在报告中标明跳过增益。峰值归一化遇到全静音也会跳过增益。非有限 dB 指标在 JSON 中为 `null`，线性峰值/RMS为0表示静音。

LUFS 首遍和复测的 `dual_mono=false`，按实际声道配置计量。单声道复制成双声道会改变 LUFS，所以不跨声道配置直接比较响度值。Peak 归一化只约束采样峰值，不等同于 true peak 限制。

## 输出保护与报告

- 所有处理结果均为 WAV PCM，可选16/24/32位；压缩输入的原始位深可能不可确定，不用浮点解码格式冒充存储位深。
- 不覆盖源文件、已有输出或已有报告；只复用内容与配置验证匹配的输出。目录输出和报告必须在输入目录之外。
- 同目录中 `a.mp3` 与 `a.wav` 都会映射为 `a.wav`，脚本会预先拒绝这种冲突。
- 输入符号链接需要用户先明确实际路径。输出先在临时文件中验证，优先用硬链接原子发布；挂载不支持时采用排他创建复制，仍拒绝覆盖已有路径。复制发布不是原子可见，消费者应等待成功事件/完成记录再读取输出；`publication_method`记录实际方式。
- 损坏文件记录错误，批量继续；不合格的临时音频清理，不会发布为最终结果。
- CLI/run单文件默认生成 `output.report.json`，批量默认生成输出目录下的 `normalization_report.json`；已有默认报告时另存唯一名称，显式 `--report` 已存在则报错。
- 报告包含配置、工具版本、脚本哈希、处理前后指标、滤镜链、实际响度模式和错误。
- LUFS 与目标相差超过1 LU时保留明确提示；输出 true peak 超限则不发布。短音频不保证精确达到综合响度目标。
- 返回码：0全部成功或复用，1存在文件失败，2全局错误，130中断。
- `--timeout` 是每个外部命令的超时秒数，默认1800。脚本依次处理文件，不同时加载整个数据集。

## 验证

```bash
python3 -B -m unittest discover -s scripts/audio_processing/normalization -p 'test_*.py' -v
```

工具不在 PATH 时，可设置 `HELIOX_FFMPEG`、`HELIOX_FFPROBE` 为可执行文件绝对路径。测试全部使用临时合成音频，不改动数据集；缺少工具时只执行路径和配置检查，音频集成测试会明确跳过。

自动测试覆盖双向重采样、抗混叠、声道映射、Peak/LUFS、静音、损坏输入，以及JSON/JSONL、配置优先级、缓存失效、发布前后中断恢复、并发调用、SDK、不覆写保护及不支持硬链接时的排他复制。音频集成验证使用FFmpeg 7.0.2，所有音频及缓存夹具均位于临时目录。

参考：[FFmpeg loudnorm](https://ffmpeg.org/ffmpeg-filters.html#loudnorm)、[FFmpeg resampler](https://ffmpeg.org/ffmpeg-resampler.html)。
