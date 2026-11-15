"""Run: python3 -m unittest discover -s scripts/audio_processing/normalization -p 'test_*.py' -v

Integration tests need ffmpeg/ffprobe on PATH or HELIOX_FFMPEG/HELIOX_FFPROBE.
All audio fixtures and outputs live in temporary directories, never the dataset.
"""

import hashlib
import errno
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import wave


SCRIPT = Path(__file__).with_name("audio_normalizer.py")
SPEC = importlib.util.spec_from_file_location("audio_normalizer", SCRIPT)
normalizer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(normalizer)
FFMPEG = os.environ.get("HELIOX_FFMPEG") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("HELIOX_FFPROBE") or shutil.which("ffprobe")


def make_wav(path, rate=16000, channels=1, duration=2.0, amplitude=0.2, antiphase=False, frequency=1000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        data = bytearray()
        for index in range(round(rate * duration)):
            value = round(32767 * amplitude * math.sin(2 * math.pi * frequency * index / rate))
            for channel in range(channels):
                data.extend(struct.pack("<h", -value if antiphase and channel == 1 else value))
        output.writeframes(data)


class PathAndConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="heliox-normalizer-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "input" / "语音 sample.wav"
        make_wav(self.source, duration=0.01)

    def arguments(self, *extra):
        return normalizer.build_parser().parse_args(["process", str(self.source), *extra])

    def test_rejects_source_overwrite_and_existing_outputs(self):
        for destination in [self.source, self.root / "existing.wav"]:
            destination.write_bytes(destination.read_bytes() if destination.exists() else b"keep")
            args = self.arguments("--output", str(destination))
            with self.assertRaises(normalizer.AudioError):
                normalizer.plan_paths(self.source, [self.source], args)

    def test_rejects_nested_output_and_same_stem_collision(self):
        directory = self.source.parent
        args = normalizer.build_parser().parse_args(["process", str(directory), "--output", str(directory / "out")])
        with self.assertRaises(normalizer.AudioError):
            normalizer.plan_paths(directory, [self.source], args)
        other = self.source.with_suffix(".flac")
        other.write_bytes(b"placeholder")
        args.output = self.root / "out"
        with self.assertRaises(normalizer.AudioError):
            normalizer.plan_paths(directory, [self.source, other], args)

    def test_rejects_symlinks_and_report_collision(self):
        link = self.root / "alias.wav"
        link.symlink_to(self.root / "missing.wav")
        args = self.arguments("--output", str(link))
        with self.assertRaises(normalizer.AudioError):
            normalizer.plan_paths(self.source, [self.source], args)
        output = self.root / "out.wav"
        args = self.arguments("--output", str(output), "--report", str(output))
        with self.assertRaises(normalizer.AudioError):
            normalizer.plan_paths(self.source, [self.source], args)

    def test_atomic_publication_never_overwrites(self):
        temporary = self.root / "temporary.wav"
        destination = self.root / "existing.wav"
        temporary.write_bytes(b"new")
        destination.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            normalizer.publish_file(temporary, destination)
        self.assertEqual(destination.read_bytes(), b"original")

    def test_rejects_nonfinite_targets_and_measurements(self):
        args = self.arguments("--output", str(self.root / "out.wav"), "--target-lufs", "nan")
        with self.assertRaises(normalizer.AudioError):
            normalizer.validate_args(args)
        args.target_lufs = -23
        normalizer.validate_args(args)
        with self.assertRaises(normalizer.AudioError):
            normalizer.loudnorm_filter(args, {"input_i": "-inf"})
        self.assertEqual(normalizer.db_to_amplitude(-math.inf), 0)

    def test_unsupported_hardlink_uses_exclusive_copy(self):
        source, destination = self.root / "temp.wav", self.root / "published.wav"
        source.write_bytes(b"complete audio bytes")
        with mock.patch.object(normalizer.os, "link", side_effect=OSError(errno.EPERM, "unsupported")):
            mode = normalizer.publish_file(source, destination)
        self.assertEqual(mode, "exclusive_copy")
        self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_exclusive_copy_does_not_overwrite_existing_target(self):
        source, destination = self.root / "temp.wav", self.root / "published.wav"
        source.write_bytes(b"new audio")
        destination.write_bytes(b"existing audio")
        with mock.patch.object(normalizer.os, "link", side_effect=OSError(errno.EPERM, "unsupported")):
            with self.assertRaises(FileExistsError):
                normalizer.publish_file(source, destination)
        self.assertEqual(destination.read_bytes(), b"existing audio")

    def test_failed_exclusive_copy_cleans_its_partial_output(self):
        source, destination = self.root / "temp.wav", self.root / "published.wav"
        source.write_bytes(b"new audio")
        def fail(source_stream, output, **kwargs):
            output.write(b"partial")
            raise KeyboardInterrupt()
        with mock.patch.object(normalizer.os, "link", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")), \
                mock.patch.object(normalizer.shutil, "copyfileobj", side_effect=fail):
            with self.assertRaises(KeyboardInterrupt):
                normalizer.publish_file(source, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(source.read_bytes(), b"new audio")

    def test_other_link_errors_do_not_trigger_copy(self):
        source, destination = self.root / "temp.wav", self.root / "published.wav"
        source.write_bytes(b"new audio")
        with mock.patch.object(normalizer.os, "link", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaises(OSError):
                normalizer.publish_file(source, destination)
        self.assertFalse(destination.exists())


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg and ffprobe are required for integration tests")
class AudioIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="heliox-normalizer-audio-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def invoke(self, *arguments, expected=0):
        command = [sys.executable, str(SCRIPT), *map(str, arguments),
                   "--ffmpeg", FFMPEG, "--ffprobe", FFPROBE, "--timeout", "60",
                   "--cache-dir", str(self.root / "cache")]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def report(self, output):
        return json.loads(output.with_suffix(".report.json").read_text())

    def test_inspection_peak_rms_lufs_and_preservation(self):
        source = self.root / "中文 空格.wav"
        make_wav(source)
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        report = self.root / "inspect.json"
        self.invoke("inspect", source, "--report", report)
        metrics = json.loads(report.read_text())["files"][0]["metrics"]
        self.assertEqual((metrics["sample_rate_hz"], metrics["channels"], metrics["bit_depth"]), (16000, 1, 16))
        self.assertAlmostEqual(metrics["duration_seconds"], 2.0, places=4)
        self.assertAlmostEqual(metrics["sample_peak"], 0.2, places=4)
        self.assertAlmostEqual(metrics["rms"], 0.2 / math.sqrt(2), places=4)
        self.assertIsNotNone(metrics["integrated_lufs"])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original)

    def test_upsampling_and_exact_channel_duplication(self):
        source, output = self.root / "mono.wav", self.root / "stereo.wav"
        make_wav(source)
        self.invoke("process", source, "--output", output, "--target-sample-rate", "48000", "--target-channels", "2")
        with wave.open(str(output), "rb") as audio:
            self.assertEqual((audio.getframerate(), audio.getnchannels()), (48000, 2))
            values = struct.unpack("<" + "h" * (audio.getnframes() * 2), audio.readframes(audio.getnframes()))
            self.assertEqual(values[::2], values[1::2])
        self.assertEqual(self.report(output)["failed"], 0)

    def test_downsample_alias_suppression_and_downmix(self):
        source, output = self.root / "96k stereo.wav", self.root / "16k mono.wav"
        make_wav(source, rate=96000, channels=2, frequency=12000)
        self.invoke("process", source, "--output", output)
        metrics = self.report(output)["files"][0]["output_metrics"]
        self.assertEqual((metrics["sample_rate_hz"], metrics["channels"]), (16000, 1))
        self.assertLess(metrics["rms"], 0.002)  # Above-Nyquist tone must not alias loudly.

    def test_peak_normalization_and_24_bit_output(self):
        source, output = self.root / "quiet.wav", self.root / "peak.wav"
        make_wav(source, amplitude=0.02)
        self.invoke("process", source, "--output", output, "--normalize", "peak", "--bit-depth", "24")
        metrics = self.report(output)["files"][0]["output_metrics"]
        self.assertEqual(metrics["bit_depth"], 24)
        self.assertAlmostEqual(metrics["sample_peak_dbfs"], -1.0, delta=0.1)

    def test_two_pass_lufs_after_channel_mapping(self):
        source, output = self.root / "quiet.wav", self.root / "lufs.wav"
        make_wav(source, duration=4, amplitude=0.02)
        self.invoke("process", source, "--output", output, "--normalize", "lufs", "--target-channels", "2")
        entry = self.report(output)["files"][0]
        self.assertAlmostEqual(entry["output_metrics"]["integrated_lufs"], -23, delta=0.5)
        self.assertLessEqual(entry["output_metrics"]["true_peak_dbtp"], -0.9)
        self.assertIn(entry["normalization_mode"], ("linear", "dynamic"))

    def test_silence_short_audio_and_explicit_skip(self):
        source = self.root / "silence.wav"
        make_wav(source, amplitude=0, duration=0.1)
        report = self.root / "inspect.json"
        self.invoke("inspect", source, "--report", report)
        metrics = json.loads(report.read_text())["files"][0]["metrics"]
        self.assertEqual(metrics["sample_peak"], 0)
        self.assertIsNone(metrics["integrated_lufs"])
        failed = self.root / "failed.wav"
        self.invoke("process", source, "--output", failed, "--normalize", "lufs", expected=1)
        self.assertFalse(failed.exists())
        output = self.root / "skip.wav"
        self.invoke("process", source, "--output", output, "--normalize", "lufs", "--unmeasurable-lufs", "skip-gain")
        self.assertEqual(self.report(output)["files"][0]["normalization_applied"], "skipped_unmeasurable_lufs")

    def test_antiphase_average_rejected_but_channel_selection_works(self):
        source, failed = self.root / "phase.wav", self.root / "failed.wav"
        make_wav(source, channels=2, antiphase=True)
        self.invoke("process", source, "--output", failed, expected=1)
        self.assertFalse(failed.exists())
        output = self.root / "left.wav"
        self.invoke("process", source, "--output", output, "--mono-source", "left")
        self.assertAlmostEqual(self.report(output)["files"][0]["output_metrics"]["sample_peak"], 0.2, places=4)

    def test_batch_corrupt_file_is_reported_without_blocking_good_file(self):
        source = self.root / "input"
        make_wav(source / "nested" / "good.wav", duration=0.5)
        (source / "corrupt.wav").write_bytes(b"not audio")
        output = self.root / "output"
        self.invoke("process", source, "--output", output, expected=1)
        report = json.loads((output / "normalization_report.json").read_text())
        self.assertEqual((report["succeeded"], report["failed"]), (1, 1))
        self.assertTrue((output / "nested" / "good.wav").exists())
        self.assertFalse((output / "corrupt.wav").exists())
        self.assertFalse(list(output.rglob(".audio-normalizer-*")))


if __name__ == "__main__":
    unittest.main()
