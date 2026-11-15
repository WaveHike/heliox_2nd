"""Shared, strict JSON configuration for the CLI and Python API.

No third-party packages are needed. YAML is deliberately unsupported: use a
JSON object with the same option names as the CLI (hyphens become underscores).
Only explicitly supplied CLI/API values should be passed as ``overrides``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any


DEFAULTS = {
    "target_sample_rate": 16000,
    "target_channels": 1,
    "bit_depth": 16,
    "resampler": "swr",
    "mono_source": "average",
    "normalize": "none",
    "peak_dbfs": -1.0,
    "target_lufs": -23.0,
    "true_peak_dbfs": -1.0,
    "lra": 7.0,
    "unmeasurable_lufs": "error",
    "timeout": 1800.0,
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe",
}

# Tool executable identity/version belongs in the cache key separately. A
# timeout changes execution policy, not the requested audio transformation.
PROCESSING_KEYS = frozenset(DEFAULTS) - {"timeout", "ffmpeg", "ffprobe"}

_INTEGER_OPTIONS = {
    "target_sample_rate": (1000, 384000),
    "target_channels": (1, 2),
}
_NUMERIC_OPTIONS = {
    "peak_dbfs": (-60.0, 0.0),
    "target_lufs": (-70.0, -5.0),
    "true_peak_dbfs": (-9.0, 0.0),
    "lra": (1.0, 50.0),
}
_CHOICES = {
    "resampler": ("swr", "soxr"),
    "mono_source": ("average", "left", "right"),
    "normalize": ("none", "peak", "lufs"),
    "unmeasurable_lufs": ("error", "skip-gain"),
}


class ConfigError(ValueError):
    """Invalid configuration, with a stable code for automated callers."""

    code = "config_error"


def _finite_float(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a finite number, not {value!r}.")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ConfigError(f"{name} must be a finite number.") from exc
    if not math.isfinite(number):
        raise ConfigError(f"{name} must be finite; NaN and Infinity are not allowed.")
    return number


def validate_config(mapping: Mapping[str, Any]) -> dict:
    """Validate a complete or partial config and return an independent copy.

    Numeric strings, booleans used as numbers, null values and unknown keys are
    errors rather than silently ignored defaults. Numeric gain/loudness options
    are normalized to floats so equivalent settings have the same fingerprint.
    A top-level ``schema_version: 1`` is accepted and stripped.
    """
    if not isinstance(mapping, Mapping):
        raise ConfigError("Configuration must be a JSON object (key/value mapping).")
    values = dict(mapping)
    if "schema_version" in values:
        version = values.pop("schema_version")
        if type(version) is not int or version != 1:
            raise ConfigError("schema_version must be the integer 1.")
    unknown = [key for key in values if key not in DEFAULTS]
    if unknown:
        names = ", ".join(repr(key) for key in unknown)
        raise ConfigError(f"Unknown configuration field(s): {names}.")
    for name, value in values.items():
        if name in _INTEGER_OPTIONS:
            low, high = _INTEGER_OPTIONS[name]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ConfigError(f"{name} must be an integer in [{low}, {high}].")
        elif name == "bit_depth":
            if isinstance(value, bool) or not isinstance(value, int) or value not in (16, 24, 32):
                raise ConfigError("bit_depth must be the integer 16, 24 or 32 (use 16, not 'PCM_16').")
        elif name in _NUMERIC_OPTIONS:
            low, high = _NUMERIC_OPTIONS[name]
            number = _finite_float(name, value)
            if not low <= number <= high:
                raise ConfigError(f"{name} must be in [{low:g}, {high:g}].")
            values[name] = number
        elif name == "timeout":
            number = _finite_float(name, value)
            if number <= 0:
                raise ConfigError("timeout must be a positive finite number of seconds.")
            values[name] = number
        elif name in _CHOICES:
            if not isinstance(value, str) or value not in _CHOICES[name]:
                raise ConfigError(f"{name} must be one of {_CHOICES[name]!r}.")
        elif name in ("ffmpeg", "ffprobe"):
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ConfigError(f"{name} must be a nonempty executable name or path without NUL characters.")
    return values


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    values = {}
    for name, value in pairs:
        if name in values:
            raise ConfigError(f"Duplicate JSON configuration field: {name!r}.")
        values[name] = value
    return values


def _reject_constant(value: str) -> None:
    raise ConfigError(f"Invalid JSON numeric constant {value!r}; use finite JSON numbers.")


def load_config(path: str | Path) -> dict:
    """Read a UTF-8 JSON config; return validated explicitly present options."""
    config_path = Path(path).expanduser()
    if config_path.suffix.lower() in (".yaml", ".yml"):
        raise ConfigError("Only JSON configuration is supported; convert YAML to a .json object.")
    try:
        content = config_path.read_text(encoding="utf-8")
        values = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Cannot read UTF-8 JSON configuration {config_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Invalid JSON configuration {config_path} at line {exc.lineno}, column {exc.colno}: "
            "use a JSON object; YAML and JSON comments are not supported."
        ) from exc
    return validate_config(values)


def resolve_config(path: str | Path | None = None, overrides: Mapping[str, Any] | None = None) -> dict:
    """Resolve ``defaults < JSON file < explicit CLI/API options``.

    None means no overrides mapping; a None option value is an error. Validate
    the file and overrides separately so an invalid field cannot be hidden by
    a higher-priority value.
    """
    values = DEFAULTS.copy()
    if path is not None:
        values.update(load_config(path))
    if overrides is not None:
        values.update(validate_config(overrides))
    return validate_config(values)


def config_fingerprint(mapping: Mapping[str, Any]) -> str:
    """SHA-256 of canonical validated JSON; the caller chooses full/subset keys."""
    values = validate_config(mapping)
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
