#!/usr/bin/env python3
r"""音频检测、重采样、声道转换与可选响度归一化（不覆盖输入）。

依赖安装
--------
Python >= 3.10，仅使用 Python 标准库，无需安装 pip 包。
需要 FFmpeg 与 ffprobe（包含 astats、loudnorm、aresample、pan 滤镜）：
  Ubuntu / Debian / WSL: sudo apt-get update && sudo apt-get install ffmpeg
  macOS:                brew install ffmpeg
  Conda:                conda install -c conda-forge ffmpeg
安装后执行 ffmpeg -version 和 ffprobe -version 确认可用。
非 PATH 安装可传 --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe。
默认使用 FFmpeg 的高质量 SWR 重采样；--resampler soxr 需要带 libsoxr 的构建。

示例
----
python3 audio_normalizer.py inspect input.wav
python3 audio_normalizer.py inspect ./input --report ./inspection.json
python3 audio_normalizer.py inspect ./input --metadata-only --json
python3 audio_normalizer.py process ./input --output ./normalized \
    --preset audio_16k_mono --json
python3 audio_normalizer.py process input.wav --output output.wav \
    --target-sample-rate 16000 --target-channels 1
python3 audio_normalizer.py process ./input --output ./normalized \
    --target-sample-rate 24000 --target-channels 1 --normalize peak --peak-dbfs -1
python3 audio_normalizer.py process ./input --output ./lufs_normalized \
    --normalize lufs --target-lufs -23 --true-peak-dbfs -1 --bit-depth 24
python3 audio_normalizer.py process mono.wav --output stereo.wav \
    --target-sample-rate 48000 --target-channels 2

约定
----
* inspect 完整解码，报告 sample peak/RMS、dBFS、integrated LUFS、true peak。
* process 输出 WAV PCM（16/24/32 位），保留输入目录的相对结构。
* 升采样不会恢复已丢失的高频；复制单声道不会恢复立体声空间信息。
* --normalize none 为默认值，不施加归一化增益；peak 与 lufs 互斥。
* 双声道到单声道默认 0.5L + 0.5R；可用 --mono-source left/right 选声道。
  相位相反的通道可能平均抵消，实际抵消会报错；应先核验声道再选策略。
* 超过两声道或多个音轨的文件可检测，但处理时拒绝自动猜测声道/音轨。
* LUFS 对静音或极短音频可能不可测；默认报错，可显式使用
  --unmeasurable-lufs skip-gain 只做格式转换，并记录跳过响度归一化。
* 输出目录必须位于输入目录之外。已有音频和报告不覆盖；相同请求且内容
  验证匹配的已有输出默认复用，返回 skipped/cache_hit。
* --config 支持严格 JSON；--preset 提供内置预设，显式参数优先。
* --json 只输出一个JSON；--jsonl逐文件输出并以summary结束。
* 默认逐文件缓存位于用户缓存目录，可用 --cache-dir 指定；--no-cache 禁用。
  --no-resume 拒绝复用已有输出。--metadata-only仅检测元数据，不解码测响度。
* Python SDK: AudioNormalizer(...).inspect()/process_file()/run()，不打印日志。
* 所有音频临时写入、检查后再发布；失败时清理临时文件并继续处理其他文件。
* 返回码：0=全部成功或缓存复用；1=音频失败；2=全局错误；130=中断。
* 缓存锁依赖POSIX fcntl，面向Linux/WSL/macOS；不支持原生Windows Python。

算法依据：https://ffmpeg.org/ffmpeg-filters.html#loudnorm
          https://ffmpeg.org/ffmpeg-resampler.html
"""

from __future__ import annotations

import argparse
import copy
import errno
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

if __package__:
    from .cache_store import CacheError, CacheStore
    from .configuration import ConfigError, DEFAULTS, PROCESSING_KEYS, config_fingerprint, resolve_config
else:
    # Direct execution and importlib-based legacy tests both remain supported.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cache_store import CacheError, CacheStore
    from configuration import ConfigError, DEFAULTS, PROCESSING_KEYS, config_fingerprint, resolve_config


VERSION = "2.0.1"
SCHEMA_VERSION = "1.0"
AUDIO_EXTENSIONS = {
    ".wav", ".wave", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus",
    ".aif", ".aiff", ".wma", ".webm", ".mp4", ".mka",
}
ASTATS = (
    "astats=metadata=0:reset=0:measure_perchannel=none:"
    "measure_overall=Peak_level+RMS_level+Number_of_samples+Number_of_NaNs+Number_of_Infs"
)


class AudioError(RuntimeError):
    """A failed decode, unsupported input, or invalid measurement."""

    def __init__(self, message: str, code: str = "audio_error"):
        super().__init__(message)
        self.code = code


def run_command(command: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Use argument lists, never a shell; bound every external process."""
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"命令超过 {timeout:g} 秒：{command[0]}", "timeout") from exc
    except OSError as exc:
        raise AudioError(f"无法执行 {command[0]}：{exc}", "dependency_error") from exc
    if result.returncode:
        raise AudioError(f"{Path(command[0]).name} 退出码 {result.returncode}：\n{result.stderr[-6000:]}", "tool_failed")
    return result


def finite_number(value) -> float | None:
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def parse_loudnorm(stderr: str) -> dict:
    """FFmpeg mixes log lines and one loudnorm JSON object on stderr."""
    for match in reversed(list(re.finditer(r'\{[^{}]*"input_i"[^{}]*\}', stderr, re.S))):
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            continue
    raise AudioError("FFmpeg 没有返回 loudnorm 测量结果；请确认滤镜可用且音频非空。")


def astats_number(stderr: str, label: str) -> float:
    matches = re.findall(re.escape(label) + r":\s*([^\r\n]+)", stderr)
    if not matches:
        raise AudioError(f"FFmpeg 缺少 astats 指标：{label}")
    try:
        return float(matches[-1].strip())
    except ValueError as exc:
        raise AudioError(f"无法解析 {label}：{matches[-1]}") from exc


def db_to_amplitude(db: float) -> float:
    if db == -math.inf:
        return 0.0
    if not math.isfinite(db):
        raise AudioError("音频幅度含非有限值。")
    return 10.0 ** (db / 20.0)


def loudnorm_filter(args, measurements: dict | None = None) -> str:
    options = [f"I={args.target_lufs:g}", f"TP={args.true_peak_dbfs:g}",
               f"LRA={args.lra:g}", "dual_mono=false", "print_format=json"]
    if measurements is not None:
        fields = {"input_i": "measured_I", "input_lra": "measured_LRA",
                  "input_tp": "measured_TP", "input_thresh": "measured_thresh",
                  "target_offset": "offset"}
        bounds = {"input_i": (-99, 0), "input_lra": (0, 99), "input_tp": (-99, 99),
                  "input_thresh": (-99, 0), "target_offset": (-99, 99)}
        for source, target in fields.items():
            value = finite_number(measurements.get(source))
            low, high = bounds[source]
            if value is None or not low <= value <= high:
                raise AudioError(f"LUFS 双遍参数无效：{source}={measurements.get(source)!r}")
            options.append(f"{target}={value:.8f}")
        # FFmpeg may fall back to dynamic mode when TP/LRA constraints require it.
        options.append("linear=true")
    return "loudnorm=" + ":".join(options)


def probe_audio(path: Path, args) -> dict:
    result = run_command([
        args.ffprobe, "-v", "error", "-select_streams", "a", "-show_entries",
        "stream=index,codec_name,sample_rate,channels,channel_layout,bits_per_sample,"
        "bits_per_raw_sample,sample_fmt,duration:format=duration", "-of", "json", str(path),
    ], args.timeout)
    try:
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams:
            raise AudioError("文件中没有音轨。")
        stream = streams[0]
        rate, channels = int(stream["sample_rate"]), int(stream["channels"])
    except (ValueError, KeyError, TypeError) as exc:
        raise AudioError("ffprobe 返回的音频元数据无效。") from exc
    if rate <= 0 or channels <= 0:
        raise AudioError("采样率和声道数必须为正数。")
    bits = None
    for field in ("bits_per_raw_sample", "bits_per_sample"):
        candidate = finite_number(stream.get(field))
        if candidate is not None and candidate > 0:
            bits = int(candidate)
            break
    # A compressed stream's fltp decoder format is NOT its storage bit depth.
    duration = finite_number(stream.get("duration"))
    if duration is None:
        duration = finite_number(data.get("format", {}).get("duration"))
    return {
        "sample_rate_hz": rate, "channels": channels, "bit_depth": bits,
        "codec": stream.get("codec_name"), "decoded_sample_format": stream.get("sample_fmt"),
        "channel_layout": stream.get("channel_layout"), "container_duration_seconds": duration,
        "audio_stream_count": len(streams), "measured_stream": "0:a:0",
    }


def decode_command(path: Path, args) -> list[str]:
    return [args.ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "info", "-xerror",
            "-i", str(path), "-map", "0:a:0", "-vn", "-sn", "-dn"]


def measure_audio(path: Path, args, sample_rate: int, filters: list[str] | None = None) -> dict:
    """Full decode; astats measures before loudnorm, which writes only to null."""
    # NaN/Inf counters are only printed by astats for floating-point input.
    measurement_format = f"aformat=sample_fmts=dbl:sample_rates={sample_rate}"
    # Bound both sides so loudnorm's 192 kHz negotiation cannot move resampling
    # ahead of astats and change its sample count, RMS or sample peak.
    chain = [*(filters or []), measurement_format, ASTATS, measurement_format, loudnorm_filter(args)]
    result = run_command(decode_command(path, args) + ["-af", ",".join(chain), "-f", "null", "-"], args.timeout)
    log = result.stderr
    for label in ("Number of NaNs", "Number of Infs"):
        if astats_number(log, label) != 0:
            raise AudioError("音频含 NaN/Infinity，拒绝继续处理。")
    samples = astats_number(log, "Number of samples")
    if not math.isfinite(samples) or samples <= 0:
        raise AudioError("音频没有可解码的样本。")
    peak_db = astats_number(log, "Peak level dB")
    rms_db = astats_number(log, "RMS level dB")
    stats = parse_loudnorm(log)
    integrated = finite_number(stats.get("input_i"))
    return {
        "duration_seconds": samples / sample_rate,
        "sample_peak": db_to_amplitude(peak_db), "sample_peak_dbfs": finite_number(peak_db),
        "rms": db_to_amplitude(rms_db), "rms_dbfs": finite_number(rms_db),
        "integrated_lufs": integrated, "true_peak_dbtp": finite_number(stats.get("input_tp")),
        "loudness_range_lu": finite_number(stats.get("input_lra")),
        "lufs_status": "measured" if integrated is not None else "unmeasurable_silence_short_or_below_gate",
        "loudnorm_first_pass": stats,
    }


def inspect_audio(path: Path, args) -> dict:
    metadata = probe_audio(path, args)
    return {**metadata, **measure_audio(path, args, metadata["sample_rate_hz"])}


def conversion_filters(metadata: dict, args) -> list[str]:
    channels = metadata["channels"]
    if metadata["audio_stream_count"] != 1 or channels not in (1, 2):
        raise AudioError("处理仅支持单音轨、单/双声道；请先明确多音轨或多声道映射。")
    filters = []
    if channels == 1 and args.target_channels == 2:
        filters.append("pan=stereo|c0=c0|c1=c0")
    elif channels == 2 and args.target_channels == 1:
        mix = {"average": "0.5*c0+0.5*c1", "left": "c0", "right": "c1"}[args.mono_source]
        filters.append(f"pan=mono|c0={mix}")
    if metadata["sample_rate_hz"] != args.target_sample_rate:
        if args.resampler == "soxr":
            filters.append(f"aresample={args.target_sample_rate}:resampler=soxr:precision=28")
        else:
            filters.append(f"aresample={args.target_sample_rate}:resampler=swr:filter_size=64:phase_shift=10")
    return filters


def publish_file(temporary: Path, destination: Path) -> str:
    """Never overwrite. Prefer atomic links; use exclusive copy on other mounts.

    Some Windows-mounted filesystems reject links and RENAME_NOREPLACE. An
    exclusive-create copy preserves no-overwrite semantics there, but is not
    atomically visible: consumers must wait for a successful result/receipt.
    A killed process may leave an incomplete file; hash verification rejects it.
    """
    try:
        os.link(temporary, destination)
        return "hardlink"
    except OSError as exc:
        if exc.errno not in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV}:
            raise
    created_identity = None
    try:
        with destination.open("xb") as output:
            created = os.fstat(output.fileno())
            created_identity = (created.st_dev, created.st_ino)
            with temporary.open("rb") as source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        return "exclusive_copy"
    except BaseException:
        # Remove only the inode this call created, never a pre-existing target
        # or a path replaced by another process while the copy was running.
        if created_identity is not None:
            try:
                current = destination.lstat()
                if (current.st_dev, current.st_ino) == created_identity:
                    destination.unlink()
            except FileNotFoundError:
                pass
        raise


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".audio-report-", suffix=".json", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        publish_file(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def process_audio(source: Path, destination: Path, args, *, inspect_fn=None, measure_fn=None,
                  before_publish=None) -> dict:
    before_stat = source.stat()
    inspect_fn = inspect_fn or (lambda path: inspect_audio(path, args))
    measure_fn = measure_fn or (lambda path, rate, filters: measure_audio(path, args, rate, filters))
    original = inspect_fn(source)
    filters = conversion_filters(original, args)
    before = measure_fn(source, args.target_sample_rate, filters) if filters else {
        key: value for key, value in original.items() if key not in (
            "sample_rate_hz", "channels", "bit_depth", "codec", "decoded_sample_format",
            "channel_layout", "container_duration_seconds", "audio_stream_count", "measured_stream")
    }
    notes = []
    if original["sample_peak"] > 0 and before["sample_peak"] == 0:
        raise AudioError("声道映射后语音完全抵消/所选通道静音；请核验并选择其他 --mono-source。")
    gain_db = None
    normalization_applied = args.normalize
    if args.normalize == "peak":
        if before["sample_peak"] == 0:
            notes.append("静音没有可归一化峰值，已跳过增益。")
            normalization_applied = "skipped_silence"
        else:
            gain_db = args.peak_dbfs - before["sample_peak_dbfs"]
            filters.append(f"volume={gain_db:.10f}dB:precision=double")
    elif args.normalize == "lufs":
        if before["integrated_lufs"] is None:
            if args.unmeasurable_lufs == "error":
                raise AudioError("LUFS 不可测（静音/过短/低于门限）。可用 --unmeasurable-lufs skip-gain 保留格式转换。")
            notes.append("LUFS 不可测，按显式配置跳过响度归一化。")
            normalization_applied = "skipped_unmeasurable_lufs"
        else:
            filters.append(loudnorm_filter(args, before["loudnorm_first_pass"]))
    if args.normalize == "none" and before["sample_peak"] > 1.0:
        raise AudioError("波形超过 PCM 满幅；请显式选择 peak/lufs 归一化以避免编码削波。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".audio-normalizer-", suffix=".wav", dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        command = decode_command(source, args)
        if filters:
            command += ["-af", ",".join(filters)]
        # loudnorm dynamic mode can run at 192 kHz: always constrain final rate.
        command += ["-ar", str(args.target_sample_rate), "-ac", str(args.target_channels),
                    "-c:a", f"pcm_s{args.bit_depth}le", "-map_metadata", "-1",
                    "-f", "wav", "-y", str(temporary)]
        conversion = run_command(command, args.timeout)
        after = inspect_fn(temporary)
        if (after["sample_rate_hz"], after["channels"], after["bit_depth"]) != (
                args.target_sample_rate, args.target_channels, args.bit_depth):
            raise AudioError("输出采样率、声道或位深与请求不一致，未发布输出。")
        tolerance = max(0.02, 2 / args.target_sample_rate)
        if abs(after["duration_seconds"] - original["duration_seconds"]) > tolerance:
            raise AudioError("输出时长发生非预期变化，未发布输出。")
        if normalization_applied == "peak":
            target_peak = 10 ** (args.peak_dbfs / 20)
            peak_tolerance = max(target_peak * (10 ** (0.1 / 20) - 1), 2 ** -(args.bit_depth - 1))
            if abs(after["sample_peak"] - target_peak) > peak_tolerance:
                raise AudioError("输出峰值未达到目标（容差0.1 dB或1个量化步长），未发布输出。")
        actual_mode = None
        if normalization_applied == "lufs":
            actual_mode = parse_loudnorm(conversion.stderr).get("normalization_type")
            if after["integrated_lufs"] is None or abs(after["integrated_lufs"] - args.target_lufs) > 1.0:
                notes.append("输出 LUFS 偏离目标超过1 LU或不可测；短音频/峰值约束可能限制对齐，请查复测结果。")
            if after["true_peak_dbtp"] is not None and after["true_peak_dbtp"] > args.true_peak_dbfs + 0.1:
                raise AudioError("最终重采样后的 true peak 超出配置上限（容差0.1 dB），未发布输出。")
        after_stat = source.stat()
        if (before_stat.st_size, before_stat.st_mtime_ns) != (after_stat.st_size, after_stat.st_mtime_ns):
            raise AudioError("处理期间输入文件发生变化，未发布输出。")
        target_met = None
        if normalization_applied == "peak":
            target_met = True
        elif normalization_applied == "lufs":
            target_met = after["integrated_lufs"] is not None and abs(after["integrated_lufs"] - args.target_lufs) <= 1.0
        elif normalization_applied.startswith("skipped_"):
            target_met = False
        result = {"input_metrics": original, "before_normalization_metrics": before,
                  "output_metrics": after, "filters": filters, "normalization_applied": normalization_applied,
                  "normalization_mode": actual_mode, "gain_db": gain_db, "notes": notes,
                  "normalization_target_met": target_met}
        if before_publish:
            before_publish(temporary, result)
        result["publication_method"] = publish_file(temporary, destination)
        return result
    finally:
        temporary.unlink(missing_ok=True)


def collect_files(root: Path) -> list[Path]:
    if root.is_symlink():
        raise AudioError("输入不能是符号链接，请显式指定实际源路径。")
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise AudioError(f"输入不存在或不是文件/目录：{root}")
    files = []
    def onerror(error):
        raise error
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
        dirs.sort()
        if any((Path(directory) / name).is_symlink() for name in dirs + names):
            raise AudioError("输入目录包含符号链接，请先明确实际数据范围。")
        files.extend(Path(directory) / name for name in sorted(names)
                     if Path(name).suffix.lower() in AUDIO_EXTENSIONS)
    return sorted(files)


def ensure_outside_input(destination: Path, root: Path) -> None:
    if root.is_dir() and destination.is_relative_to(root.resolve()):
        raise AudioError("输出和报告必须位于输入目录之外，避免混入源数据。")
    if not root.is_dir() and destination == root.resolve():
        raise AudioError("输出/报告不能与源文件相同。")


def plan_paths(root: Path, files: list[Path], args, *, allow_existing=False) -> tuple[list[Path | None], Path | None]:
    destinations: list[Path | None] = [None] * len(files)
    if args.command == "process":
        if args.output.expanduser().is_symlink():
            raise AudioError("输出路径不能是符号链接。")
        output = args.output.expanduser().resolve()
        ensure_outside_input(output, root)
        if root.is_dir():
            if output.exists() and not output.is_dir():
                raise AudioError("批量处理的 --output 必须为目录。")
            destinations = [(output / path.relative_to(root)).with_suffix(".wav") for path in files]
        else:
            if output.suffix.lower() != ".wav":
                raise AudioError("单文件 --output 必须明确指定 .wav 文件名。")
            destinations = [output]
        default_report = output / "normalization_report.json" if root.is_dir() else output.with_suffix(".report.json")
    else:
        default_report = None
    if args.report and args.report.expanduser().is_symlink():
        raise AudioError("报告路径不能是符号链接。")
    report = (args.report.expanduser().resolve() if args.report else default_report)
    if report is not None and not args.report and os.path.lexists(report):
        report = report.with_name(f"{report.stem}.{uuid.uuid4().hex}{report.suffix}")
    for destination in destinations:
        if destination is not None and destination.is_symlink():
            raise AudioError(f"输出路径不能是符号链接：{destination}", "path_error")
    targets = [p.resolve() for p in destinations if p is not None]
    if report is not None:
        targets.append(report)
    # Reject case-only collisions as well, for Windows-mounted datasets.
    keys = [str(p).casefold() for p in targets]
    if len(set(keys)) != len(keys):
        raise AudioError("输出路径冲突（例如同目录 a.wav 与 a.mp3），请先区分源文件名。")
    for target in targets:
        ensure_outside_input(target, root)
        if os.path.lexists(target) and (not allow_existing or target == report or not target.is_file()):
            raise AudioError(f"拒绝覆盖已有输出或报告：{target}", "output_conflict")
    for i, target in enumerate(targets):
        if any(other in target.parents for j, other in enumerate(targets) if i != j):
            raise AudioError("音频输出与报告路径存在文件/目录冲突。")
    return destinations, report


def fingerprint_file(path: Path) -> dict:
    """Content identity; mtime/size only detect concurrent modification."""
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise AudioError(f"读取时文件发生变化：{path}", "input_changed")
    return {"sha256": digest.hexdigest(), "size_bytes": after.st_size}


def implementation_fingerprint() -> str:
    digest = hashlib.sha256()
    for name in ("audio_normalizer.py", "configuration.py", "cache_store.py"):
        digest.update(name.encode())
        digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()


def validate_cached_metrics(metrics, operation):
    """Treat corrupted records as errors, never as successful measurements."""
    common = {"duration_seconds", "sample_peak", "sample_peak_dbfs", "rms", "rms_dbfs",
              "integrated_lufs", "true_peak_dbtp", "loudness_range_lu", "lufs_status", "loudnorm_first_pass"}
    if not isinstance(metrics, dict) or not common <= metrics.keys():
        raise CacheError("测量缓存缺少必需字段。")
    if operation != "transformed_measurement":
        required = {"sample_rate_hz", "channels", "bit_depth", "codec", "decoded_sample_format",
                    "channel_layout", "container_duration_seconds", "audio_stream_count", "measured_stream",
                    "measurement_level"}
        if not required <= metrics.keys():
            raise CacheError("测量缓存缺少元数据字段。")
        for field in ("sample_rate_hz", "channels", "audio_stream_count"):
            if type(metrics[field]) is not int or metrics[field] <= 0:
                raise CacheError(f"测量缓存字段无效：{field}")
        if metrics["bit_depth"] is not None and (type(metrics["bit_depth"]) is not int or metrics["bit_depth"] <= 0):
            raise CacheError("缓存位深无效。")
    for field in common - {"lufs_status", "loudnorm_first_pass"}:
        value = metrics[field]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)):
            raise CacheError(f"测量缓存包含无效数值：{field}")
    if operation == "metadata":
        if metrics["measurement_level"] != "metadata" or metrics["lufs_status"] != "not_measured":
            raise CacheError("元数据缓存的测量范围不匹配。")
    else:
        if not isinstance(metrics["loudnorm_first_pass"], dict):
            raise CacheError("测量缓存的响度记录无效。")
        if metrics["duration_seconds"] is None or metrics["duration_seconds"] <= 0:
            raise CacheError("测量缓存时长无效。")
        for field in ("sample_peak", "rms"):
            if metrics[field] is None or metrics[field] < 0:
                raise CacheError("测量缓存幅度无效。")
        if operation == "inspect" and metrics["measurement_level"] != "full":
            raise CacheError("缓存不是完整测量。")


def validate_cached_result(payload, fingerprint):
    if not isinstance(payload.get("result"), dict) or not isinstance(payload.get("output_fingerprint"), dict):
        raise CacheError("处理缓存结构无效。")
    result = payload["result"]
    required = set(empty_result(Path("input"), Path("output"))) | {
        "filters", "before_normalization_metrics", "normalization_applied", "normalization_mode", "gain_db"}
    if not required <= result.keys() or result["schema_version"] != SCHEMA_VERSION or result["status"] != "ok":
        raise CacheError("处理缓存缺少必需字段或版本不匹配。")
    if result["input_fingerprint"] != fingerprint or result["output_fingerprint"] != payload["output_fingerprint"]:
        raise CacheError("处理缓存指纹不一致。")
    if not isinstance(result["notes"], list) or not isinstance(result["filters"], list):
        raise CacheError("处理缓存notes/filters字段无效。")
    validate_cached_metrics(result["input_metrics"], "inspect")
    validate_cached_metrics(result["output_metrics"], "inspect")
    validate_cached_metrics(result["before_normalization_metrics"], "transformed_measurement")


def empty_result(source: Path, output: Path | None = None) -> dict:
    return {"schema_version": SCHEMA_VERSION, "operation": "process" if output else "inspect",
            "status": "ok", "reason": None, "error_code": None, "error": None,
            "input": str(source), "output": str(output) if output else None,
            "input_fingerprint": None, "output_fingerprint": None,
            "metrics": None, "input_metrics": None, "output_metrics": None,
            "normalization_target_met": None, "notes": [],
            "cache_hit": False, "recovered": False}


def error_envelope(exc: Exception, command: str | None, code: int = 2) -> dict:
    return {"schema_version": SCHEMA_VERSION, "script_version": VERSION,
            "implementation_hash": None, "command": command, "status": "error",
            "exit_code": code, "error_code": getattr(exc, "code", "io_error"), "error": str(exc),
            "started_at_utc": None, "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "python_version": sys.version, "tools": {}, "config": None, "config_hash": None,
            "config_path": None, "runtime": None, "report_path": None,
            "files_total": 0, "succeeded": 0, "processed": 0, "skipped": 0, "failed": 0, "files": []}


class AudioNormalizer:
    """Shared SDK/CLI engine. Methods return dictionaries and never print.

    Config precedence: built-in defaults < JSON file < keyword overrides.
    inspect/process_file raise typed errors; run collects per-file failures.
    Use as a context manager, or call close(), to release the SQLite connection.
    """

    def __init__(self, config=None, *, cache_dir=None, use_cache=True, resume=True, **overrides):
        if not isinstance(use_cache, bool) or not isinstance(resume, bool):
            raise ConfigError("use_cache 和 resume 必须为布尔值。")
        self.config = MappingProxyType(resolve_config(config, overrides))
        self.config_hash = config_fingerprint(self.config)
        self.config_path = str(Path(config).expanduser().resolve()) if config else None
        default_cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "audio-normalizer"
        self.cache_dir = Path(cache_dir or os.environ.get("AUDIO_NORM_CACHE_DIR", default_cache)).expanduser().absolute()
        self.use_cache, self.resume = use_cache, resume
        self._store = None
        self._tool_info = {}
        self._args = SimpleNamespace(**self.config)
        self._implementation = implementation_fingerprint()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    def close(self):
        if self._store is not None:
            self._store.close()
            self._store = None

    @property
    def cache(self):
        if self.use_cache and self._store is None:
            self._store = CacheStore(self.cache_dir)
        return self._store

    def _validate_cache_scope(self, root: Path, destinations=()):
        if not self.use_cache:
            return
        resolved = self.cache_dir.resolve()
        ensure_outside_input(resolved, root)
        if root.resolve().is_relative_to(resolved):
            raise AudioError("输入不能位于工具缓存目录中。", "path_error")
        for destination in destinations:
            if destination is not None and (destination.resolve().is_relative_to(resolved)
                                            or resolved.is_relative_to(destination.resolve())):
                raise AudioError("缓存与输出/报告文件路径不能相互包含。", "path_error")

    def _tools(self, metadata_only=False) -> dict:
        names = ("ffprobe",) if metadata_only else ("ffmpeg", "ffprobe")
        for name in names:
            if name not in self._tool_info:
                executable = shutil.which(self.config[name])
                if executable is None:
                    raise AudioError(f"缺少 {name}，请安装 FFmpeg 或指定 --{name}。", "dependency_error")
                version = run_command([executable, "-version"], self.config["timeout"]).stdout
                self._tool_info[name] = {"path": str(Path(executable).resolve()),
                                         "version": version.splitlines()[0],
                                         "build_hash": hashlib.sha256(version.encode()).hexdigest()}
                setattr(self._args, name, executable)
        return {name: self._tool_info[name] for name in names}

    def _key(self, operation, fingerprint, *, metadata_only=False, extra=None):
        signal_config = {name: self.config[name] for name in sorted(PROCESSING_KEYS)}
        if operation != "process":
            signal_config = {} if metadata_only else {
                name: self.config[name] for name in ("target_lufs", "true_peak_dbfs", "lra")}
        request = {"operation": operation, "input": fingerprint,
                   "implementation": self._implementation, "config": signal_config,
                   "tools": self._tools(metadata_only), "extra": extra}
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _source(path) -> Path:
        path = Path(path).expanduser().absolute()
        files = collect_files(path)
        if not path.is_file() or len(files) != 1:
            raise AudioError("此 SDK 方法只接受单个文件；批量请调用 run()。", "path_error")
        return path.resolve()

    def _cached_measurement(self, path: Path, *, metadata_only=False, filters=None, sample_rate=None):
        fingerprint = fingerprint_file(path)
        operation = "transformed_measurement" if filters is not None else ("metadata" if metadata_only else "inspect")
        key = self._key(operation, fingerprint, metadata_only=metadata_only,
                        extra={"filters": filters, "sample_rate": sample_rate})
        store = self.cache
        with store.lock("measurement:" + key) if store else nullcontext():
            cached = store.get(key) if store else None
            if cached is not None:
                payload = cached["payload"]
                if cached["state"] != "complete" or not isinstance(payload.get("metrics"), dict):
                    raise CacheError("测量缓存结构无效。")
                validate_cached_metrics(payload["metrics"], operation)
                if fingerprint_file(path) != fingerprint:
                    raise AudioError("缓存读取期间输入发生变化。", "input_changed")
                return copy.deepcopy(payload["metrics"]), True, fingerprint
            if filters is not None:
                metrics = measure_audio(path, self._args, sample_rate, filters)
            elif metadata_only:
                metrics = {**probe_audio(path, self._args), "duration_seconds": None,
                           "sample_peak": None, "sample_peak_dbfs": None, "rms": None, "rms_dbfs": None,
                           "integrated_lufs": None, "true_peak_dbtp": None, "loudness_range_lu": None,
                           "lufs_status": "not_measured", "measurement_level": "metadata",
                           "loudnorm_first_pass": None}
                metrics["duration_seconds"] = metrics["container_duration_seconds"]
            else:
                metrics = {**inspect_audio(path, self._args), "measurement_level": "full"}
            if fingerprint_file(path) != fingerprint:
                raise AudioError("分析期间输入发生变化，结果未缓存。", "input_changed")
            if store:
                store.put(key, {"metrics": metrics})
            return metrics, False, fingerprint

    def inspect(self, path, *, metadata_only=False) -> dict:
        if not isinstance(metadata_only, bool):
            raise ConfigError("metadata_only必须为布尔值。")
        source = self._source(path)
        self._validate_cache_scope(source)
        metrics, hit, fingerprint = self._cached_measurement(source, metadata_only=metadata_only)
        result = empty_result(source)
        result.update(metrics=metrics, input_fingerprint=fingerprint, cache_hit=hit,
                      status="skipped" if hit else "ok", reason="cache_hit" if hit else None)
        return result

    def process_file(self, input_path, output_path) -> dict:
        source = self._source(input_path)
        args = SimpleNamespace(command="process", output=Path(output_path), report=None)
        destinations, _ = plan_paths(source, [source], args, allow_existing=True)
        destination = destinations[0]
        self._validate_cache_scope(source, [destination])
        fingerprint = fingerprint_file(source)
        key = self._key("process", fingerprint, extra={"destination": str(destination)})
        store = self.cache
        # Lock by destination, not config: different requests cannot race to it.
        with store.lock("output:" + str(destination)) if store else nullcontext():
            cached = store.get(key) if store else None
            exists = os.path.lexists(destination)
            if exists:
                if not self.resume or cached is None or destination.is_symlink() or not destination.is_file():
                    raise AudioError(f"已有输出无法按当前请求安全复用：{destination}", "output_conflict")
                payload = cached["payload"]
                validate_cached_result(payload, fingerprint)
                if fingerprint_file(destination) != payload["output_fingerprint"]:
                    raise AudioError(f"输出内容已变化：{destination}", "output_conflict")
                if fingerprint_file(source) != fingerprint:
                    raise AudioError("输入在复用检查期间发生变化。", "input_changed")
                result = copy.deepcopy(payload["result"])
                result.update(input=str(source), output=str(destination), status="skipped", reason="cache_hit",
                              cache_hit=True, recovered=cached["state"] == "prepared")
                if cached["state"] == "prepared":
                    store.put(key, payload, state="complete")
                return result

            prepared = None
            def before_publish(temporary, details):
                nonlocal prepared
                if fingerprint_file(source) != fingerprint:
                    raise AudioError("处理期间输入内容发生变化，未发布输出。", "input_changed")
                result = empty_result(source, destination)
                output_fp = fingerprint_file(temporary)
                result.update(details, input_fingerprint=fingerprint, output_fingerprint=output_fp)
                prepared = {"result": result, "output_fingerprint": output_fp}
                # Durable receipt BEFORE publication recovers a crash after link().
                if store:
                    store.put(key, prepared, state="prepared")

            published_result = process_audio(
                source, destination, self._args,
                inspect_fn=lambda path: self._cached_measurement(path)[0],
                measure_fn=lambda path, rate, filters: self._cached_measurement(
                    path, filters=filters, sample_rate=rate)[0],
                before_publish=before_publish,
            )
            prepared["result"].update(published_result)
            if store:
                store.put(key, prepared, state="complete")
            return prepared["result"]

    def run(self, input_path, *, output=None, command="inspect", metadata_only=False,
            report=None, on_result=None) -> dict:
        if not isinstance(metadata_only, bool):
            raise ConfigError("metadata_only必须为布尔值。")
        if command not in ("inspect", "process") or (command == "process" and (output is None or metadata_only)):
            raise ConfigError("run需要合法command；process需要output且不能metadata_only。")
        root = Path(input_path).expanduser().absolute()
        files = collect_files(root)
        if not files:
            raise AudioError("没有发现音频文件。", "input_error")
        args = SimpleNamespace(command=command, output=Path(output) if output else None,
                               report=Path(report) if report else None)
        destinations, report_path = plan_paths(root, files, args, allow_existing=True)
        self._validate_cache_scope(root, [*destinations, report_path])
        self._tools(metadata_only)
        started = datetime.now(timezone.utc).isoformat()
        records = []
        for source, destination in zip(files, destinations):
            try:
                result = (self.inspect(source, metadata_only=metadata_only) if command == "inspect"
                          else self.process_file(source, destination))
            except (AudioError, CacheError, OSError, ValueError) as exc:
                result = empty_result(source, destination)
                result.update(status="error", error_code=getattr(exc, "code", "io_error"), error=str(exc))
            records.append(result)
            if on_result:
                on_result(copy.deepcopy(result))
        failed = sum(item["status"] == "error" for item in records)
        skipped = sum(item["status"] == "skipped" for item in records)
        envelope = {
            "schema_version": SCHEMA_VERSION, "script_version": VERSION,
            "implementation_hash": self._implementation, "command": command,
            "status": "success" if not failed else ("partial_failure" if failed < len(records) else "error"),
            "exit_code": 1 if failed else 0, "error_code": None, "error": None,
            "started_at_utc": started, "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "python_version": sys.version, "tools": copy.deepcopy(self._tool_info),
            "config": dict(self.config), "config_hash": self.config_hash, "config_path": self.config_path,
            "runtime": {"cache_enabled": self.use_cache, "resume": self.resume,
                        "cache_dir": str(self.cache_dir) if self.use_cache else None, "metadata_only": metadata_only},
            "files_total": len(records), "succeeded": len(records) - failed,
            "processed": len(records) - failed - skipped, "skipped": skipped, "failed": failed,
            "report_path": str(report_path) if report_path else None, "files": records,
        }
        if report_path:
            try:
                write_report(report_path, envelope)
            except FileExistsError:
                if report is not None:
                    raise
                # Another run may have published the default report since planning.
                report_path = report_path.with_name(f"{report_path.stem}.{uuid.uuid4().hex}{report_path.suffix}")
                envelope["report_path"] = str(report_path)
                write_report(report_path, envelope)
        return envelope


class CLIParser(argparse.ArgumentParser):
    def error(self, message):
        raise AudioError(message, "cli_error")


def build_parser() -> argparse.ArgumentParser:
    parser = CLIParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "process"):
        sub = commands.add_parser(name, help="检测音频" if name == "inspect" else "转换、复测并可验证续跑")
        sub.add_argument("input", type=Path, help="单个文件或递归扫描目录")
        config_choice = sub.add_mutually_exclusive_group()
        config_choice.add_argument("--config", type=Path, help="JSON配置：显式CLI参数覆盖配置，配置覆盖默认值")
        config_choice.add_argument("--preset", choices=("audio_16k_mono", "audio_16k_mono_lufs23"),
                                   help="内置JSON预设，不依赖当前工作目录")
        sub.add_argument("--report", type=Path, help="显式报告路径不可覆盖；process默认生成新报告")
        output_format = sub.add_mutually_exclusive_group()
        output_format.add_argument("--json", action="store_true", help="stdout只输出一个JSON对象")
        output_format.add_argument("--jsonl", action="store_true", help="逐文件输出JSONL，最后一行为summary")
        sub.add_argument("--cache-dir", type=Path, help="默认AUDIO_NORM_CACHE_DIR或用户缓存目录")
        sub.add_argument("--no-cache", action="store_true", help="禁用缓存；已有输出不可复用")
        sub.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True,
                         help="默认验证内容与配置后复用已完成输出；--no-resume禁用")
        for field in ("ffmpeg", "ffprobe", "resampler", "mono_source", "normalize", "unmeasurable_lufs"):
            sub.add_argument("--" + field.replace("_", "-"), default=argparse.SUPPRESS)
        for field in ("timeout", "target_lufs", "true_peak_dbfs", "lra", "peak_dbfs"):
            sub.add_argument("--" + field.replace("_", "-"), type=float, default=argparse.SUPPRESS)
        for field in ("target_sample_rate", "target_channels", "bit_depth"):
            sub.add_argument("--" + field.replace("_", "-"), type=int, default=argparse.SUPPRESS)
        if name == "inspect":
            sub.add_argument("--metadata-only", action="store_true", help="仅ffprobe元数据，不解码/测响度")
        else:
            sub.add_argument("--output", required=True, type=Path)
    return parser


def validate_args(args) -> None:
    """Kept for callers of the previous function; all entry points share validation."""
    try:
        values = resolve_config(getattr(args, "config", None), {k: v for k, v in vars(args).items() if k in DEFAULTS})
    except ConfigError as exc:
        raise AudioError(str(exc), "config_error") from exc
    for key, value in values.items():
        setattr(args, key, value)


def main(argv=None) -> int:
    tokens = list(sys.argv[1:] if argv is None else argv)
    jsonl_mode = "--jsonl" in tokens
    json_mode = "--json" in tokens and not jsonl_mode
    # Output flags work before or after the subcommand; other options follow it.
    tokens = [t for t in tokens if t not in ("--json", "--jsonl")] + [
        t for t in tokens if t in ("--json", "--jsonl")]
    command = None
    try:
        args = build_parser().parse_args(tokens)
        command = args.command
        if args.preset:
            args.config = Path(__file__).resolve().parent / "configs" / f"{args.preset}.json"
        overrides = {key: value for key, value in vars(args).items() if key in DEFAULTS}
        def emit(result):
            if jsonl_mode:
                print(json.dumps({"type": "file", **result}, ensure_ascii=False, allow_nan=False), flush=True)
            elif not json_mode:
                metrics = result["metrics"] or result["output_metrics"] or {}
                if result["status"] == "error":
                    print(f"[ERROR] {result['input']}: {result['error']}", file=sys.stderr)
                else:
                    print(f"[{result['status'].upper()}] {result['input']} | "
                          f"{metrics.get('sample_rate_hz')} Hz | {metrics.get('channels')} ch | "
                          f"{metrics.get('duration_seconds')} s | peak={metrics.get('sample_peak')} | "
                          f"RMS={metrics.get('rms')} | LUFS={metrics.get('integrated_lufs')}")
                    for note in result["notes"]:
                        print(f"  [NOTE] {note}", file=sys.stderr)
        with AudioNormalizer(args.config, cache_dir=args.cache_dir, use_cache=not args.no_cache,
                             resume=args.resume, **overrides) as normalizer:
            envelope = normalizer.run(args.input, output=getattr(args, "output", None), command=command,
                                      metadata_only=getattr(args, "metadata_only", False),
                                      report=args.report, on_result=emit)
        if json_mode:
            print(json.dumps(envelope, ensure_ascii=False, allow_nan=False))
        elif jsonl_mode:
            print(json.dumps({**envelope, "type": "summary", "files": []}, ensure_ascii=False, allow_nan=False), flush=True)
        else:
            print(f"完成：{envelope['processed']} 新执行，{envelope['skipped']} 缓存命中，{envelope['failed']} 失败。")
            if envelope["report_path"]:
                print(f"报告：{envelope['report_path']}")
        return envelope["exit_code"]
    except (AudioError, ConfigError, CacheError, OSError, ValueError) as exc:
        envelope = error_envelope(exc, command)
        if json_mode or jsonl_mode:
            if jsonl_mode:
                envelope["type"] = "summary"
            print(json.dumps(envelope, ensure_ascii=False, allow_nan=False), flush=True)
        else:
            print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        envelope = error_envelope(AudioError("操作中断；逐文件记录可用于续跑。", "interrupted"), command, 130)
        if json_mode or jsonl_mode:
            if jsonl_mode:
                envelope["type"] = "summary"
            print(json.dumps(envelope, ensure_ascii=False, allow_nan=False), flush=True)
        else:
            print(envelope["error"], file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
