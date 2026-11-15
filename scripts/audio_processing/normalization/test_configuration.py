"""Configuration contracts; run with unittest discovery in this directory."""

import json
import math
import tempfile
import unittest
from pathlib import Path

try:
    from .configuration import (
        DEFAULTS, PROCESSING_KEYS, ConfigError, config_fingerprint, load_config,
        resolve_config, validate_config,
    )
except ImportError:
    from configuration import (
        DEFAULTS, PROCESSING_KEYS, ConfigError, config_fingerprint, load_config,
        resolve_config, validate_config,
    )


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "preset.json"

    def write_config(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_precedence_and_no_mutation(self):
        self.write_config({"target_sample_rate": 24000, "normalize": "lufs", "target_lufs": -20})
        overrides = {"target_sample_rate": 48000, "normalize": "none"}
        result = resolve_config(self.path, overrides)
        self.assertEqual(result["target_sample_rate"], 48000)
        self.assertEqual(result["normalize"], "none")
        self.assertEqual(result["target_lufs"], -20.0)
        self.assertEqual(result["bit_depth"], 16)
        self.assertEqual(DEFAULTS["target_sample_rate"], 16000)
        self.assertEqual(overrides, {"target_sample_rate": 48000, "normalize": "none"})
        result["bit_depth"] = 24
        self.assertEqual(resolve_config()["bit_depth"], 16)

    def test_schema_version(self):
        self.write_config({"schema_version": 1, "normalize": "none"})
        self.assertEqual(load_config(self.path), {"normalize": "none"})
        for value in (True, 1.0, "1", 2, None):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                validate_config({"schema_version": value})

    def test_unknown_fields_and_error_code(self):
        with self.assertRaises(ConfigError) as raised:
            validate_config({"target_sr": 16000})
        self.assertEqual(raised.exception.code, "config_error")
        self.write_config({"target_sample_rate": "invalid"})
        with self.assertRaises(ConfigError):
            resolve_config(self.path, {"target_sample_rate": 16000})

    def test_strict_types_and_values(self):
        invalid = {
            "target_sample_rate": [True, 16000.0, "16000", None, 999, 384001],
            "target_channels": [True, 1.0, 0, 3, None],
            "bit_depth": [True, 16.0, "PCM_16", 8, None],
            "normalize": [True, None, "loudness", []],
            "resampler": ["linear", None],
            "mono_source": ["mix", None],
            "unmeasurable_lufs": ["skip", None],
            "peak_dbfs": [True, "-1", None, math.nan, math.inf, -61, 1],
            "target_lufs": [True, None, -71, -4],
            "true_peak_dbfs": [True, None, -10, 1],
            "lra": [True, None, 0, 51],
            "timeout": [True, None, 0, -1, math.inf],
            "ffmpeg": [None, 1, "", " ", "ff\x00mpeg"],
            "ffprobe": [None, False, ""],
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                    validate_config({key: value})

    def test_numeric_boundaries_and_large_numbers(self):
        self.assertEqual(validate_config({"target_lufs": -70, "lra": 50}), {"target_lufs": -70.0, "lra": 50.0})
        self.assertEqual(validate_config({"target_sample_rate": 384000}), {"target_sample_rate": 384000})
        with self.assertRaises(ConfigError):
            validate_config({"timeout": 10 ** 400})

    def test_json_constants_duplicate_keys_and_wrong_roots(self):
        for content in (
            '{"target_lufs": NaN}', '{"timeout": Infinity}', '{"timeout": -Infinity}',
            '{"target_lufs": 1e999}', '{"normalize": "none", "normalize": "peak"}',
            '[]', 'null', '16000', '"text"',
        ):
            with self.subTest(content=content), self.assertRaises(ConfigError):
                self.path.write_text(content, encoding="utf-8")
                load_config(self.path)

    def test_json_only_explanation_and_missing_file(self):
        self.path.write_text("target_sample_rate: 16000\n", encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "JSON.*YAML"):
            load_config(self.path)
        with self.assertRaisesRegex(ConfigError, "JSON.*YAML"):
            load_config(self.path.with_suffix(".yaml"))
        with self.assertRaises(ConfigError):
            load_config(self.path.with_name("missing.json"))

    def test_canonical_fingerprint_and_processing_keys(self):
        first = {"target_sample_rate": 16000, "target_lufs": -23}
        second = {"target_lufs": -23.0, "target_sample_rate": 16000, "schema_version": 1}
        self.assertEqual(config_fingerprint(first), config_fingerprint(second))
        self.assertNotEqual(config_fingerprint(first), config_fingerprint({**first, "target_sample_rate": 24000}))
        self.assertEqual(len(config_fingerprint(first)), 64)
        self.assertEqual(PROCESSING_KEYS, set(DEFAULTS) - {"timeout", "ffmpeg", "ffprobe"})

    def test_shipped_presets(self):
        directory = Path(__file__).parent / "configs"
        plain = resolve_config(directory / "audio_16k_mono.json")
        loudness = resolve_config(directory / "audio_16k_mono_lufs23.json")
        self.assertEqual(plain["normalize"], "none")
        self.assertEqual(loudness["normalize"], "lufs")
        for config in (plain, loudness):
            self.assertEqual(config["target_sample_rate"], 16000)
            self.assertEqual(config["target_channels"], 1)
            self.assertEqual(config["bit_depth"], 16)
        self.assertEqual((loudness["target_lufs"], loudness["true_peak_dbfs"], loudness["lra"]), (-23.0, -1.0, 7.0))


if __name__ == "__main__":
    unittest.main()
