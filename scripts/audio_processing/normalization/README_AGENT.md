# Audio Normalizer：自动化调用契约

这是项目内工具说明，不是安装到 Agent 平台的 Skill。调用方应依照退出码、`status`、`error_code` 和 JSON 字段判断结果，不解析中文日志，也不要假设说明文件会被其他 Agent 自动发现。

## 入口与依赖

- CLI：本目录 `audio-norm`，或 `python3 /绝对路径/audio_normalizer.py`。
- Python：`scripts.audio_processing.normalization.audio_normalizer.AudioNormalizer`。
- Python 3.10+、FFmpeg、ffprobe；Python 部分仅使用标准库。缓存锁使用 POSIX `fcntl`，面向 Linux/WSL 等 POSIX 环境。
- 完整检测及处理需要 FFmpeg 的 `astats`、`loudnorm`、`aresample`、`pan`。`--resampler soxr` 另需 FFmpeg 编译时启用 libsoxr。
- `inspect --metadata-only` 只调用 ffprobe，不测响度、不完整解码；内容哈希仍读取文件，不能将其视为零读取开销。
- 不安装依赖、不下载权重、不修改 Shell 配置；工具发现失败时用 `--ffmpeg` 和 `--ffprobe` 指定现有可执行文件。

以下入口不依赖当前工作目录；输入输出请换成实际路径：

```bash
audio_norm_dir=/mnt/d/heliox_2nd/scripts/audio_processing/normalization

# 单文件完整检测，stdout 是一个 JSON 对象。
"$audio_norm_dir/audio-norm" inspect /path/to/input.wav --json

# 文件夹递归、快速元数据检测。
"$audio_norm_dir/audio-norm" inspect /path/to/input_dir --metadata-only --json

# 16 kHz、单声道、PCM 16；默认不施加响度增益。
"$audio_norm_dir/audio-norm" process /path/to/input_dir \
  --output /path/to/output_dir \
  --config "$audio_norm_dir/configs/audio_16k_mono.json" --json

# 显式指定现有工具位置和可写缓存位置；逐条流式输出 JSONL。
"$audio_norm_dir/audio-norm" process /path/to/input.wav \
  --output /path/to/output.wav \
  --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe \
  --cache-dir /path/to/audio_cache --jsonl
```

`inspect` / `process` 是必需的子命令，输入是位置参数；不存在 `--input_dir` / `--output_dir`。除 `--json` / `--jsonl` 可以放在子命令前外，其他选项放在子命令后。`--help` 输出帮助文本，不作为 JSON API 使用。

## 配置

只支持 UTF-8 JSON 对象，不支持 YAML。优先级为 **显式 CLI 参数或 SDK 关键字 > 配置文件 > 默认值**。未知键、重复键、null、布尔值冒充数字、非有限数字、错误类型或范围都会报 `config_error`；不会静默回退。

配置键使用下划线，CLI 使用连字符；SDK 使用同一个配置键。例如 `target_sample_rate` 对应 `--target-sample-rate`，没有 `target_sr` 别名。

| 配置键 | 默认值 | 约束 |
|---|---|---|
| `target_sample_rate` | `16000` | 整数，1000–384000 Hz |
| `target_channels` | `1` | 整数 1 或 2 |
| `bit_depth` | `16` | 整数 16、24 或 32；不接受 `"PCM_16"` |
| `resampler` | `"swr"` | `swr` / `soxr` |
| `mono_source` | `"average"` | `average` / `left` / `right` |
| `normalize` | `"none"` | `none` / `peak` / `lufs` |
| `peak_dbfs` | `-1.0` | −60 到 0 |
| `target_lufs` | `-23.0` | −70 到 −5 |
| `true_peak_dbfs` | `-1.0` | −9 到 0，实际单位 dBTP |
| `lra` | `7.0` | 1 到 50 LU |
| `unmeasurable_lufs` | `"error"` | `error` / `skip-gain` |
| `timeout` | `1800.0` | 每条外部命令的正有限超时秒数 |
| `ffmpeg` / `ffprobe` | 同名命令 | 非空字符串，可指定可执行文件路径 |

可选配置字段 `schema_version` 必须为整数 `1`，加载后移除；它与输出的字符串 `schema_version: "1.0"` 不同。

两个预设：

- `configs/audio_16k_mono.json`：仅格式规范化，`normalize="none"`。
- `configs/audio_16k_mono_lufs23.json`：相同格式，双遍 LUFS 归一化，目标 −23 LUFS、true peak 上限 −1 dBTP、LRA 7 LU。

也可直接使用 `--preset audio_16k_mono` 或 `--preset audio_16k_mono_lufs23`，按脚本所在位置定位配置；它与 `--config` 互斥。快捷入口的解释器可通过 `AUDIO_NORM_PYTHON` 指定。

**−23 LUFS 不是所有 TTS 模型的统一要求。** 调用方应根据目标模型流水线显式选用预设。升采样不恢复高频，复制 Mono 不恢复真实立体声信息。

## JSON / JSONL 输出

`--json` 的 stdout 是单个完整 JSON envelope；单文件和文件夹均使用 `files` 数组。`--jsonl` 每完成一个文件输出一行 `{"type":"file", ...逐文件字段...}`，最后输出一行 `{"type":"summary", ...envelope字段..., "files":[]}`。两种选项互斥。stderr 不属于结构化协议，不要合并 stderr 后再解析 stdout。

正常完成时 envelope 的稳定关键字段：

| 字段 | 类型及含义 |
|---|---|
| `schema_version` | 字符串 `"1.0"` |
| `command` | `inspect` / `process` |
| `status` | `success` / `partial_failure` / `error` |
| `exit_code` | 整数，与 CLI 退出码一致 |
| `error_code`, `error` | 全局错误代码、说明；无全局错误为 null |
| `files_total`, `succeeded`, `processed`, `skipped`, `failed` | 整数计数；`succeeded` 包含缓存复用，`processed` 指本次新执行，包括检测 |
| `files` | 逐文件记录数组；JSONL 最后摘要为空数组，避免重复传输 |
| `config`, `config_hash`, `config_path` | 最终生效配置、SHA-256、配置文件绝对路径或 null |
| `runtime` | `cache_enabled`、`resume`、`cache_dir`、`metadata_only` |
| `tools` | ffmpeg/ffprobe 的路径、版本、构建信息哈希 |
| `script_version`, `implementation_hash` | 脚本版本及实现指纹 |
| `started_at_utc`, `finished_at_utc`, `python_version` | 执行来源信息 |
| `report_path` | 保存的报告路径或 null |

全局启动错误或中断保留相同顶层字段，尚未建立的配置、运行信息、起始时间等为null，tools为空对象。其计数为0，不代表此前没有处理过文件；JSONL已输出的文件事件和缓存记录仍可保留。强制终止进程可能没有最后摘要。

逐文件稳定关键字段：

| 字段 | 类型及含义 |
|---|---|
| `schema_version`, `operation` | `"1.0"`；`inspect` / `process` |
| `status` | `ok` / `skipped` / `error`；缓存复用为 `skipped` |
| `reason`, `cache_hit` | 缓存命中时为 `"cache_hit"`、true；其他为 null、false |
| `input`, `output` | 绝对路径；inspect 的 output 为 null |
| `input_fingerprint`, `output_fingerprint` | `sha256`、`size_bytes` 对象，未生成时为 null |
| `metrics` | inspect 指标对象；process 时为 null |
| `input_metrics`, `output_metrics` | process 前后指标；inspect 或失败时可以为 null |
| `normalization_target_met` | true / false / null；null 表示没有要求归一化或未完成测量 |
| `notes` | 字符串数组，包含未达响度目标、跳过增益等说明 |
| `error_code`, `error` | 文件错误代码及说明，无错误为 null |
| `recovered` | 是否从发布后尚未完成提交的记录中核验恢复 |

process 成功还包含 `before_normalization_metrics`、`filters`、`normalization_applied`、`normalization_mode` 和 `gain_db`，供复核信号处理。`normalization_applied` 可能是 `none`、`peak`、`lufs`、`skipped_silence` 或 `skipped_unmeasurable_lufs`。

指标名按实际代码使用，不能替换成 `sr`、`meta_before` 等别名：

- 格式：`sample_rate_hz`、`channels`、`bit_depth`、`codec`、`decoded_sample_format`、`channel_layout`。
- 时长：`duration_seconds`、`container_duration_seconds`；仅元数据模式使用容器时长，未验证完整可解码。
- 电平：`sample_peak`、`sample_peak_dbfs`、`rms`、`rms_dbfs`。RMS 汇总所有声道，不是每声道数组。
- 响度：`integrated_lufs`、`true_peak_dbtp`、`loudness_range_lu`、`lufs_status`。
- 检测级别：`measurement_level="full"` / `"metadata"`；仅元数据检测的电平/响度为 null，`lufs_status="not_measured"`。
- 音轨：`audio_stream_count`、`measured_stream`。检测以 `0:a:0` 为测量音轨。
- `loudnorm_first_pass` 是 FFmpeg 双遍处理诊断数据，不建议作为上层业务接口。

不可测 LUFS 用 null 和 `lufs_status` 说明原因；静音线性幅度为 0，对应 dBFS 为 null。压缩音频的存储位深可能未知，不能用解码 sample format 冒充位深。

`status="ok"` 只表示处理成功，**不保证达到响度目标**。必须同时检查 `normalization_target_met`、`normalization_applied`、`notes`。LUFS 目标容差为 1 LU；静音跳过增益时 `normalization_target_met=false`。

## 退出码与错误

| 退出码 | 含义 |
|---|---|
| 0 | 全部文件成功或经核验复用 |
| 1 | 至少一个文件失败；可检查 `files` 中其他成功项 |
| 2 | 命令、配置、路径、依赖、报告等全局错误 |
| 130 | 捕获到 KeyboardInterrupt；可用同一请求续跑 |

当前 `error_code` 包括 `cli_error`、`config_error`、`dependency_error`、`timeout`、`tool_failed`、`audio_error`、`input_error`、`path_error`、`input_changed`、`output_conflict`、`cache_error`、`io_error`、`interrupted`。某些旧路径校验仍使用通用 `audio_error`。根据代码字段分类，不匹配错误文案；未知错误代码也必须视为失败。

## 缓存、续跑与副作用

- 默认启用缓存和续跑。缓存目录优先级：`--cache-dir` / SDK 参数 > `AUDIO_NORM_CACHE_DIR` > `$XDG_CACHE_HOME/audio-normalizer` > `~/.cache/audio-normalizer`。
- inspect 也会写缓存，内容为 SQLite 记录和锁文件，不写回输入。受限环境应明确指定输入范围之外的可写缓存目录。
- 缓存标识包括输入内容 SHA-256、测量/处理阶段、有效信号参数、实现指纹及工具身份；原始与变换后的测量分开保存。
- 复用输出必须同时存在匹配记录、相同请求和内容哈希一致的输出；mtime、文件名或“输出已存在”不能单独作为复用依据。
- `--no-resume` 拒绝复用已有音频输出，但仍可复用测量缓存。`--no-cache` 禁止缓存读写，也就无法核验复用已有输出；它不是覆盖开关。
- 文件成功后逐项提交记录；重新执行相同命令复用已完成项，缺失输出或未完成项重新处理。不能只凭 `status="skipped"` 推断实际执行过本次转换。
- 不修改输入音频。process 只生成 WAV PCM 16/24/32 位，新输出先验证再发布，不覆盖已有音频。
- 单文件 output 必须是 `.wav` 路径；目录 input 对应目录 output，保留相对结构。不同输入映射到同名 WAV 时拒绝执行。
- 输出、报告、缓存不得混入输入目录；输入不能是缓存目录内的文件。拒绝输入符号链接以及输入目录中的符号链接，拒绝指定输出/报告为符号链接。
- 双声道到 Mono 默认平均；可选择左右声道。多音轨或超过两声道可以检测，处理会拒绝隐式映射。
- 未知、被修改或配置不匹配的已有输出报 `output_conflict`，不会替调用方删除或覆盖。缓存损坏也不会被视为可信命中。
- process 默认保存报告：单文件 `output.report.json`，批量 `output_dir/normalization_report.json`；默认报告已有时生成带唯一后缀的新报告。显式 `--report` 已存在则拒绝覆盖。
- inspect 默认只返回结果；需要持久报告时传 `--report`。JSONL 只影响 stdout，磁盘报告仍是完整 JSON。
- 发布优先用硬链接；挂载不支持时使用排他创建复制，已有目标仍拒绝覆盖。复制方式不是原子可见；消费者必须等待成功结果/完成记录后再读取输出。`publication_method`为`hardlink`或`exclusive_copy`；从发布中断记录恢复时可能尚无此字段。可捕获的复制失败会清理本次创建的不完整文件；强制终止若留下部分文件，后续哈希校验会拒绝复用，不会自动覆盖或删除。
- 不自动清理历史缓存或音频。删除缓存会失去已有输出的续跑凭据；需保留时使用稳定缓存目录。

## Python API

项目根目录必须位于 Python 导入路径中，例如在项目根运行，或由宿主应用设置 `PYTHONPATH=/mnt/d/heliox_2nd`。不依赖交互 Shell alias。

```python
from scripts.audio_processing.normalization.audio_normalizer import AudioNormalizer

with AudioNormalizer(
    config="/mnt/d/heliox_2nd/scripts/audio_processing/normalization/configs/audio_16k_mono.json",
    cache_dir="/path/to/audio_cache",
    use_cache=True,
    resume=True,
    target_sample_rate=16000,  # 显式关键字覆盖 JSON
) as normalizer:
    record = normalizer.inspect("/path/to/input.wav", metadata_only=False)
    converted = normalizer.process_file("/path/to/input.wav", "/path/to/output.wav")
    report = normalizer.run(
        "/path/to/input_dir",
        command="process",
        output="/path/to/output_dir",
        # 可选 report="/path/to/unique_report.json",
        # 可选 on_result=callback，每条结果完成后回调，参数为记录副本。
    )
```

构造器：`AudioNormalizer(config=None, *, cache_dir=None, use_cache=True, resume=True, **overrides)`；`config` 是 JSON 文件路径，`overrides` 仅接受上表配置键。

- `inspect(path, *, metadata_only=False)`：单文件，返回逐文件记录。
- `process_file(input_path, output_path)`：单文件，返回逐文件记录；本方法不单独写汇总报告。
- `run(input_path, *, output=None, command="inspect", metadata_only=False, report=None, on_result=None)`：文件或目录，返回 envelope；process 不能搭配 metadata_only。
- `close()` 或上下文管理器释放缓存连接。

SDK 自身不打印，也不退出 Python 进程。构造和单文件方法会抛出 `ConfigError`、`AudioError`、`CacheError` 或文件系统异常；这些自定义异常有稳定 `.code`。`run()` 将逐文件错误收集进结果，启动/路径/报告等全局错误仍抛出。回调抛出的异常不会自动变成音频错误；调用方负责回调逻辑。
