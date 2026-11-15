"""Small, durable, process-safe cache for the audio normalization tools.

Only Python's standard library is required. Payloads are JSON objects, never
pickle; callers remain responsible for validating their audio fingerprints.
The ``prepared`` state lets a caller commit the planned output fingerprint
before publishing an output, then mark the record ``complete`` afterwards.

POSIX ``flock`` locks serialize operations with the same identity. Keep the
entire lookup/compute/publish/commit sequence inside ``store.lock(identity)``.
SQLite transactions themselves stay short, so unrelated audio can run in
parallel without holding a database transaction during an FFmpeg invocation.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import threading


SCHEMA_VERSION = 1
VALID_STATES = frozenset({"prepared", "complete"})


class CacheError(Exception):
    """A cache operation failed; callers must not treat it as a cache hit."""

    code = "cache_error"


def _reject_constant(value):
    raise ValueError(f"Non-finite JSON value: {value}")


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"Non-finite JSON number: {value}")
    return parsed


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_text(value, name):
    if not isinstance(value, str) or not value:
        raise CacheError(f"{name} must be a non-empty string")


class CacheStore:
    """Versioned SQLite records and identity locks in an explicit directory.

    ``get`` returns ``None`` for an absent key, otherwise
    ``{'state': 'prepared'|'complete', 'payload': <dict>}``. Corruption, invalid
    states, unsupported schema versions, and unsafe paths raise ``CacheError``.
    An existing output must never be trusted merely because a record exists.
    """

    def __init__(self, directory):
        self.directory = Path(directory).expanduser().absolute()
        self.database_path = self.directory / "cache.sqlite3"
        self.locks_directory = self.directory / "locks"
        self._mutex = threading.RLock()
        self._connection = None
        try:
            self._check_directory(self.directory, create=True)
            self._check_directory(self.locks_directory, create=True)
            self._check_database_paths()
            self._connection = sqlite3.connect(
                str(self.database_path), timeout=30.0, isolation_level=None,
                check_same_thread=False,
            )
            self._connection.execute("PRAGMA busy_timeout = 30000")
            # DELETE avoids persistent WAL sidecars; EXTRA also synchronizes
            # journal deletion, in addition to FULL's commit durability.
            self._connection.execute("PRAGMA journal_mode = DELETE")
            self._connection.execute("PRAGMA synchronous = EXTRA")
            self._initialize_schema()
        except (OSError, sqlite3.Error, CacheError) as exc:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            if isinstance(exc, CacheError):
                raise
            raise CacheError(f"Cannot open cache {self.directory}: {exc}") from exc

    @staticmethod
    def _check_directory(path, create=False):
        if path.is_symlink():
            raise CacheError(f"Cache directories cannot be symbolic links: {path}")
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink() or not path.is_dir():
            raise CacheError(f"Cache directory is missing or unsafe: {path}")

    @staticmethod
    def _check_regular_path(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            raise CacheError(f"Cache files must be regular files, not links: {path}")

    def _check_database_paths(self):
        self._check_directory(self.directory)
        for suffix in ("", "-journal", "-wal", "-shm"):
            self._check_regular_path(Path(str(self.database_path) + suffix))

    def _initialize_schema(self):
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            if version == 0 and not tables:
                connection.execute(
                    "CREATE TABLE records ("
                    "key TEXT PRIMARY KEY NOT NULL, "
                    "state TEXT NOT NULL CHECK (state IN ('prepared', 'complete')), "
                    "payload_json TEXT NOT NULL)"
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise CacheError(
                    f"Unsupported cache schema {version}; expected {SCHEMA_VERSION}"
                )
            elif tables != {"records"}:
                raise CacheError("Cache schema does not match its declared version")
            columns = connection.execute("PRAGMA table_info(records)").fetchall()
            # Validate names, affinity, nullability and primary key position.
            expected = [
                ("key", "TEXT", 1, 1),
                ("state", "TEXT", 1, 0),
                ("payload_json", "TEXT", 1, 0),
            ]
            actual = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
            if actual != expected:
                raise CacheError("Cache records table has an incompatible schema")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def _require_open(self):
        if self._connection is None:
            raise CacheError("CacheStore is closed")

    def get(self, key):
        """Return a validated persisted record, or ``None`` when absent."""
        _validate_text(key, "Cache key")
        with self._mutex:
            self._require_open()
            try:
                self._check_database_paths()
                row = self._connection.execute(
                    "SELECT state, payload_json FROM records WHERE key = ?", (key,)
                ).fetchone()
                if row is None:
                    return None
                state, encoded = row
                if state not in VALID_STATES:
                    raise CacheError(f"Invalid persisted cache state for {key}")
                payload = json.loads(
                    encoded, parse_constant=_reject_constant,
                    parse_float=_finite_float, object_pairs_hook=_unique_object,
                )
                if not isinstance(payload, dict):
                    raise CacheError(f"Cache payload for {key} is not a JSON object")
                return {"state": state, "payload": payload}
            except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError) as exc:
                raise CacheError(f"Cannot read cache record {key}: {exc}") from exc

    def put(self, key, payload, state="complete"):
        """Atomically commit a JSON object in either supported state."""
        _validate_text(key, "Cache key")
        if not isinstance(state, str) or state not in VALID_STATES:
            raise CacheError(f"Invalid cache state: {state!r}")
        if not isinstance(payload, dict):
            raise CacheError("Cache payload must be a JSON object")
        try:
            encoded = json.dumps(
                payload, ensure_ascii=False, allow_nan=False,
                sort_keys=True, separators=(",", ":"),
            )
        except (ValueError, TypeError, RecursionError) as exc:
            raise CacheError(f"Cache payload is not strict JSON: {exc}") from exc
        with self._mutex:
            self._require_open()
            try:
                self._check_database_paths()
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    self._connection.execute(
                        "INSERT INTO records (key, state, payload_json) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET "
                        "state = excluded.state, payload_json = excluded.payload_json",
                        (key, state, encoded),
                    )
                    self._connection.execute("COMMIT")
                except BaseException:
                    self._connection.execute("ROLLBACK")
                    raise
            except (OSError, sqlite3.Error) as exc:
                raise CacheError(f"Cannot commit cache record {key}: {exc}") from exc

    @contextmanager
    def lock(self, identity):
        """Exclusively lock one operation across processes until block exit.

        Lock files deliberately remain in place: unlinking them could let two
        processes lock different inodes for the same identity. Closing the
        descriptor, including on process termination, releases the POSIX lock.
        """
        _validate_text(identity, "Lock identity")
        self._require_open()
        descriptor = None
        try:
            self._check_directory(self.directory)
            self._check_directory(self.locks_directory)
            name = hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".lock"
            path = self.locks_directory / name
            self._check_regular_path(path)
            descriptor = os.open(
                path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise CacheError(f"Lock is not a regular file: {path}")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except (OSError, CacheError) as exc:
            if descriptor is not None:
                os.close(descriptor)
            if isinstance(exc, CacheError):
                raise
            raise CacheError(f"Cannot acquire cache lock: {exc}") from exc
        try:
            yield
        finally:
            # Closing also unlocks, including when the protected operation fails.
            os.close(descriptor)

    def close(self):
        """Close the database connection; repeated calls are harmless."""
        with self._mutex:
            if self._connection is not None:
                try:
                    self._connection.close()
                except sqlite3.Error as exc:
                    raise CacheError(f"Cannot close cache: {exc}") from exc
                finally:
                    self._connection = None

    def __enter__(self):
        self._require_open()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
