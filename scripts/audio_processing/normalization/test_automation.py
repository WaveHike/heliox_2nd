"""Automation contract tests. All fixtures, cache, reports and outputs are temporary."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from test_audio_normalizer import FFMPEG, FFPROBE, SCRIPT, make_wav, normalizer
import test_audio_normalizer as fixtures


class MachineOutputTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run([sys.executable, "-B", str(SCRIPT), *map(str, args)],
                              capture_output=True, text=True, timeout=30)

    def test_argument_errors_are_machine_readable(self):
        for args in [("--json", "process"), ("inspect", "x.wav", "--bad-option", "--json"),
                     ("process", "x.wav", "--output", "y.wav", "--target-channels", "xyz", "--json")]:
            result = self.invoke(*args)
            self.assertEqual(result.returncode, 2)
            record = json.loads(result.stdout)
            self.assertEqual(record["error_code"], "cli_error")
            self.assertEqual(record["files"], [])
            self.assertNotIn("Traceback", result.stderr)


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/ffprobe required")
class AutomationTests(unittest.TestCase):
    setUp = fixtures.AudioIntegrationTests.setUp
    invoke = fixtures.AudioIntegrationTests.invoke
    def sdk(self, **kwargs):
        return normalizer.AudioNormalizer(cache_dir=self.root / "cache", ffmpeg=FFMPEG,
                                           ffprobe=FFPROBE, timeout=60, **kwargs)

    def test_json_is_clean_and_missing_dependency_is_structured(self):
        source = self.root / "audio.wav"
        make_wav(source, duration=0.5)
        result = self.invoke("inspect", source, "--json")
        report = json.loads(result.stdout)
        self.assertEqual(report["schema_version"], "1.0")
        self.assertEqual(report["status"], "success")
        self.assertEqual(report["files"][0]["metrics"]["channels"], 1)
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "inspect", str(source),
                                 "--json", "--ffmpeg", str(self.root / "missing-ffmpeg")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error_code"], "dependency_error")

    def test_jsonl_partial_failure_and_config_precedence(self):
        directory = self.root / "input"
        make_wav(directory / "good.wav", duration=0.5)
        (directory / "bad.wav").write_bytes(b"broken")
        config = self.root / "config.json"
        config.write_text(json.dumps({"target_sample_rate": 24000, "target_channels": 2}))
        result = self.invoke("process", directory, "--output", self.root / "out", "--config", config,
                             "--target-channels", "1", "--jsonl", expected=1)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([event["type"] for event in events], ["file", "file", "summary"])
        self.assertEqual(events[-1]["status"], "partial_failure")
        self.assertEqual(events[-1]["files"], [])
        good = next(event for event in events[:-1] if event["status"] == "ok")
        self.assertEqual((good["output_metrics"]["sample_rate_hz"], good["output_metrics"]["channels"]), (24000, 1))
        config.write_text('{"typo": 1}')
        result = self.invoke("inspect", directory, "--config", config, "--json", expected=2)
        self.assertEqual(json.loads(result.stdout)["error_code"], "config_error")

    def test_sdk_is_silent_and_inspection_cache_avoids_decode(self):
        source = self.root / "audio.wav"
        make_wav(source)
        stream = io.StringIO()
        with self.sdk() as sdk, contextlib.redirect_stdout(stream):
            first = sdk.inspect(source)
            with mock.patch.object(normalizer, "measure_audio", side_effect=AssertionError("decoded again")):
                second = sdk.inspect(source)
            self.assertEqual(first["status"], "ok")
            self.assertEqual(second["status"], "skipped")
            self.assertEqual(first["metrics"], second["metrics"])
        self.assertEqual(stream.getvalue(), "")

    def test_metadata_only_separate_from_full_and_needs_no_ffmpeg(self):
        source = self.root / "audio.wav"
        make_wav(source, duration=0.5)
        with normalizer.AudioNormalizer(cache_dir=self.root / "cache", ffprobe=FFPROBE,
                                       ffmpeg="/missing/ffmpeg") as sdk:
            record = sdk.inspect(source, metadata_only=True)
            self.assertIsNone(record["metrics"]["sample_peak"])
            self.assertEqual(record["metrics"]["lufs_status"], "not_measured")
        with self.sdk() as sdk:
            full = sdk.inspect(source)
            self.assertEqual(full["status"], "ok")
            self.assertIsNotNone(full["metrics"]["sample_peak"])

    def test_source_change_same_size_and_mtime_invalidates(self):
        source = self.root / "audio.wav"
        make_wav(source, amplitude=0.2)
        with self.sdk() as sdk:
            first = sdk.inspect(source)
            original = source.stat()
            make_wav(source, amplitude=0.3)
            os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
            self.assertEqual(source.stat().st_size, original.st_size)
            second = sdk.inspect(source)
            self.assertEqual(second["status"], "ok")
            self.assertNotEqual(first["input_fingerprint"], second["input_fingerprint"])

    def test_resume_after_partial_batch_and_new_report(self):
        directory, output = self.root / "in", self.root / "out"
        make_wav(directory / "a.wav", duration=0.5)
        make_wav(directory / "b.wav", duration=0.5, amplitude=0.3)
        with self.sdk() as sdk:
            sdk.process_file(directory / "a.wav", output / "a.wav")
        with self.sdk() as sdk:
            resumed = sdk.run(directory, output=output, command="process")
            self.assertEqual((resumed["processed"], resumed["skipped"]), (1, 1))
            with mock.patch.object(normalizer, "process_audio", side_effect=AssertionError("processed again")):
                repeat = sdk.run(directory, output=output, command="process")
            self.assertEqual(repeat["skipped"], 2)
            self.assertNotEqual(resumed["report_path"], repeat["report_path"])
            self.assertTrue(Path(resumed["report_path"]).exists())

    def test_config_source_and_output_changes_never_overwrite(self):
        source, output = self.root / "source.wav", self.root / "out.wav"
        make_wav(source, duration=0.5)
        with self.sdk() as sdk:
            sdk.process_file(source, output)
        before = output.read_bytes()
        with self.sdk(target_channels=2) as sdk:
            with self.assertRaises(normalizer.AudioError) as error:
                sdk.process_file(source, output)
            self.assertEqual(error.exception.code, "output_conflict")
        self.assertEqual(output.read_bytes(), before)
        make_wav(source, duration=0.5, amplitude=0.3)
        with self.sdk() as sdk:
            with self.assertRaises(normalizer.AudioError):
                sdk.process_file(source, output)
        make_wav(source, duration=0.5)
        damaged = bytearray(output.read_bytes())
        damaged[-1] ^= 1
        output.write_bytes(damaged)
        with self.sdk() as sdk:
            with self.assertRaises(normalizer.AudioError):
                sdk.process_file(source, output)
        self.assertEqual(output.read_bytes(), damaged)

    def test_unknown_existing_output_no_resume_and_no_cache(self):
        source, output = self.root / "source.wav", self.root / "out.wav"
        make_wav(source, duration=0.5)
        output.write_bytes(b"existing unrelated file")
        with self.sdk() as sdk:
            with self.assertRaises(normalizer.AudioError):
                sdk.process_file(source, output)
        self.assertEqual(output.read_bytes(), b"existing unrelated file")
        output.unlink()
        with self.sdk() as sdk:
            sdk.process_file(source, output)
        for options in ({"resume": False}, {"use_cache": False}):
            with self.sdk(**options) as sdk:
                with self.assertRaises(normalizer.AudioError):
                    sdk.process_file(source, output)

    def test_crash_after_publication_recovers_prepared_receipt(self):
        source, output = self.root / "source.wav", self.root / "out.wav"
        make_wav(source, duration=0.5)
        publish = normalizer.publish_file
        def interrupted(temp, destination):
            publish(temp, destination)
            raise KeyboardInterrupt()
        with self.sdk() as sdk, mock.patch.object(normalizer, "publish_file", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                sdk.process_file(source, output)
        self.assertTrue(output.exists())
        with self.sdk() as sdk, mock.patch.object(normalizer, "process_audio", side_effect=AssertionError("reencoded")):
            recovered = sdk.process_file(source, output)
            self.assertTrue(recovered["recovered"])
            self.assertEqual(recovered["status"], "skipped")

    def test_malformed_cache_payload_is_error_not_traceback(self):
        source = self.root / "source.wav"
        make_wav(source, duration=0.5)
        with self.sdk() as sdk:
            sdk.inspect(source)
            key = sdk._key("inspect", normalizer.fingerprint_file(source),
                           extra={"filters": None, "sample_rate": None})
            sdk.cache.put(key, {"metrics": {}})
        result = self.invoke("inspect", source, "--json", expected=1)
        self.assertEqual(json.loads(result.stdout)["files"][0]["error_code"], "cache_error")

    def test_explicit_existing_report_and_cache_nested_in_file_rejected(self):
        source = self.root / "source.wav"
        make_wav(source, duration=0.5)
        report = self.root / "existing.json"
        report.write_text('"keep"')
        self.invoke("inspect", source, "--report", report, "--json", expected=2)
        self.assertEqual(report.read_text(), '"keep"')
        destination = self.root / "out.wav"
        with normalizer.AudioNormalizer(cache_dir=destination / "cache", ffmpeg=FFMPEG, ffprobe=FFPROBE) as sdk:
            with self.assertRaises(normalizer.AudioError):
                sdk.process_file(source, destination)
        self.assertFalse(destination.exists())

    def test_default_report_collision_at_publication_uses_new_path(self):
        source, output = self.root / "source.wav", self.root / "out.wav"
        make_wav(source, duration=0.5)
        original = normalizer.write_report
        calls = []
        def collide_once(path, report):
            calls.append(path)
            if len(calls) == 1:
                path.write_text('"concurrent report"')
                raise FileExistsError(path)
            return original(path, report)
        with self.sdk() as sdk, mock.patch.object(normalizer, "write_report", side_effect=collide_once):
            report = sdk.run(source, output=output, command="process")
        self.assertEqual(report["status"], "success")
        self.assertEqual(calls[0].read_text(), '"concurrent report"')
        self.assertEqual(report["report_path"], str(calls[1]))

    def test_builtin_preset_is_relative_to_script_and_cli_overrides(self):
        source, output = self.root / "source.wav", self.root / "out.wav"
        make_wav(source, duration=0.5)
        result = self.invoke("process", source, "--output", output, "--preset", "audio_16k_mono",
                             "--target-sample-rate", "24000", "--json")
        report = json.loads(result.stdout)
        self.assertEqual(report["config"]["target_sample_rate"], 24000)
        self.assertEqual(report["files"][0]["output_metrics"]["sample_rate_hz"], 24000)

    def test_missing_output_recreated_and_corrupt_process_receipt_rejected(self):
        source, output = self.root / "source.wav", self.root / "out.wav"
        make_wav(source, duration=0.5)
        with self.sdk() as sdk:
            first = sdk.process_file(source, output)
            output.unlink()
            second = sdk.process_file(source, output)
            self.assertEqual(second["status"], "ok")
            self.assertEqual(first["output_fingerprint"], second["output_fingerprint"])
            key = sdk._key("process", normalizer.fingerprint_file(source), extra={"destination": str(output)})
            sdk.cache.put(key, {"result": {}, "output_fingerprint": first["output_fingerprint"]})
            with self.assertRaises(normalizer.CacheError):
                sdk.process_file(source, output)

    def test_prepared_but_unpublished_output_is_recomputed(self):
        source, output = self.root / "source.wav", self.root / "out.wav"
        make_wav(source, duration=0.5)
        with self.sdk() as sdk, mock.patch.object(normalizer, "publish_file", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                sdk.process_file(source, output)
        self.assertFalse(output.exists())
        with self.sdk() as sdk:
            record = sdk.process_file(source, output)
        self.assertEqual(record["status"], "ok")

    def test_tool_and_implementation_identity_changes_prevent_output_reuse(self):
        source, output = self.root / "source.wav", self.root / "out.wav"
        make_wav(source, duration=0.5)
        with self.sdk() as sdk:
            sdk.process_file(source, output)
            sdk._tool_info["ffmpeg"]["build_hash"] = "changed-build"
            with self.assertRaises(normalizer.AudioError) as error:
                sdk.process_file(source, output)
            self.assertEqual(error.exception.code, "output_conflict")
        with self.sdk() as sdk:
            sdk._implementation = "changed-implementation"
            with self.assertRaises(normalizer.AudioError):
                sdk.process_file(source, output)

    def test_two_concurrent_cli_runs_share_output_and_save_separate_reports(self):
        source, output = self.root / "source.wav", self.root / "out.wav"
        make_wav(source, duration=1.0)
        command = [sys.executable, "-B", str(SCRIPT), "process", str(source), "--output", str(output),
                   "--json", "--ffmpeg", FFMPEG, "--ffprobe", FFPROBE, "--cache-dir", str(self.root / "cache")]
        jobs = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        reports = []
        for job in jobs:
            stdout, stderr = job.communicate(timeout=60)
            self.assertEqual(job.returncode, 0, stdout + stderr)
            reports.append(json.loads(stdout))
        self.assertEqual(sorted(report["files"][0]["status"] for report in reports), ["ok", "skipped"])
        self.assertNotEqual(reports[0]["report_path"], reports[1]["report_path"])

    def test_project_launcher_and_package_import_work(self):
        source = self.root / "source.wav"
        make_wav(source, duration=0.5)
        command = [str(SCRIPT.with_name("audio-norm")), "inspect", str(source), "--json", "--metadata-only",
                   "--ffprobe", FFPROBE, "--cache-dir", str(self.root / "cache")]
        result = subprocess.run(command, cwd="/tmp", capture_output=True, text=True,
                                env={**os.environ, "AUDIO_NORM_PYTHON": sys.executable}, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["files"][0]["metrics"]["measurement_level"], "metadata")
        code = "from scripts.audio_processing.normalization.audio_normalizer import AudioNormalizer; print(AudioNormalizer.__name__)"
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=SCRIPT.parents[3],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "AudioNormalizer")


if __name__ == "__main__":
    unittest.main()
