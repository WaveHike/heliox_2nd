"""Cache tests use temporary files only; no audio dataset is accessed."""

from contextlib import closing
import hashlib
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("cache_store.py")
SPEC = importlib.util.spec_from_file_location("normalization_cache_store", MODULE_PATH)
cache_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache_module)
CacheStore = cache_module.CacheStore
CacheError = cache_module.CacheError


class CacheStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="heliox-cache-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "cache"

    def test_cross_instance_visibility_and_prepared_persistence(self):
        with CacheStore(self.directory) as first, CacheStore(self.directory) as second:
            self.assertIsNone(first.get("new"))
            first.put("new", {"path": "中文.wav", "fingerprint": "abc"}, state="prepared")
            self.assertEqual(second.get("new"), {
                "state": "prepared", "payload": {"path": "中文.wav", "fingerprint": "abc"},
            })
        with CacheStore(self.directory) as reopened:
            self.assertEqual(reopened.get("new")["state"], "prepared")
            reopened.put("new", {"completed": True})
            self.assertEqual(reopened.get("new"), {
                "state": "complete", "payload": {"completed": True},
            })

    def test_invalid_writes_do_not_replace_a_good_record(self):
        with CacheStore(self.directory) as store:
            store.put("existing", {"value": 1})
            for payload, state in [({}, "failed"), ([], "complete"),
                                   ({"x": float("nan")}, "complete"),
                                   ({"x": float("inf")}, "complete")]:
                with self.subTest(payload=payload, state=state):
                    with self.assertRaises(CacheError) as error:
                        store.put("existing", payload, state=state)
                    self.assertEqual(error.exception.code, "cache_error")
                    self.assertEqual(store.get("existing")["payload"], {"value": 1})

    def test_invalid_persisted_json_is_never_a_hit(self):
        with CacheStore(self.directory) as store:
            for encoded in ["broken", "[]", '{"x":NaN}', '{"x":Infinity}',
                            '{"x":1e9999}', '{"x":1,"x":2}']:
                with self.subTest(encoded=encoded):
                    with closing(sqlite3.connect(store.database_path)) as database, database:
                        database.execute(
                            "INSERT OR REPLACE INTO records VALUES (?, ?, ?)",
                            ("bad", "complete", encoded),
                        )
                    with self.assertRaises(CacheError):
                        store.get("bad")

    def test_unknown_schema_is_rejected_without_replacing_data(self):
        self.directory.mkdir()
        database_path = self.directory / "cache.sqlite3"
        with closing(sqlite3.connect(database_path)) as database, database:
            database.execute("PRAGMA user_version = 99")
            database.execute("CREATE TABLE preserved (value TEXT)")
            database.execute("INSERT INTO preserved VALUES ('original')")
        with self.assertRaises(CacheError):
            CacheStore(self.directory)
        with closing(sqlite3.connect(database_path)) as database, database:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 99)
            self.assertEqual(database.execute("SELECT value FROM preserved").fetchone()[0], "original")

    def test_zero_version_with_existing_table_is_rejected(self):
        self.directory.mkdir()
        with closing(sqlite3.connect(self.directory / "cache.sqlite3")) as database, database:
            database.execute("CREATE TABLE unrelated (value TEXT)")
        with self.assertRaises(CacheError):
            CacheStore(self.directory)

    def test_symlinks_are_rejected(self):
        real = self.root / "real"
        real.mkdir()
        self.directory.symlink_to(real, target_is_directory=True)
        with self.assertRaises(CacheError):
            CacheStore(self.directory)
        self.directory.unlink()
        for index, name in enumerate(["cache.sqlite3", "cache.sqlite3-journal", "locks"]):
            with self.subTest(name=name):
                directory = self.root / f"symlink-case-{index}"
                directory.mkdir()
                link = directory / name
                link.symlink_to(self.root / "absent")
                with self.assertRaises(CacheError):
                    CacheStore(directory)
                self.assertFalse((self.root / "absent").exists())
                link.unlink()

    def test_lock_symlink_rejected_and_lock_released_after_error(self):
        with CacheStore(self.directory) as store:
            identity = "output:/example.wav"
            lock_path = store.locks_directory / (hashlib.sha256(identity.encode()).hexdigest() + ".lock")
            lock_path.symlink_to(self.root / "absent")
            with self.assertRaises(CacheError):
                with store.lock(identity):
                    self.fail("Entered a symbolic-link lock")
            lock_path.unlink()
            with self.assertRaisesRegex(RuntimeError, "test body"):
                with store.lock(identity):
                    raise RuntimeError("test body")
            with store.lock(identity):
                store.put("recovered", {"ok": True})

    def test_same_identity_is_serialized_between_processes(self):
        child_code = """
import sys
sys.path.insert(0, sys.argv[1])
from cache_store import CacheStore
with CacheStore(sys.argv[2]) as store:
    print('ready', flush=True)
    with store.lock('shared-output'):
        print('acquired', flush=True)
"""
        with CacheStore(self.directory) as store:
            with store.lock("shared-output"):
                child = subprocess.Popen(
                    [sys.executable, "-c", child_code, str(MODULE_PATH.parent), str(self.directory)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                self.addCleanup(lambda: child.kill() if child.poll() is None else None)
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.assertRaises(subprocess.TimeoutExpired):
                    child.communicate(timeout=0.2)
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "acquired")

    def test_different_identities_do_not_block(self):
        with CacheStore(self.directory) as first, CacheStore(self.directory) as second:
            with first.lock("output-one"):
                with second.lock("output-two"):
                    second.put("other", {"ok": True})
            self.assertEqual(first.get("other")["payload"], {"ok": True})

    def test_close_is_idempotent_and_closed_access_is_rejected(self):
        store = CacheStore(self.directory)
        store.close()
        store.close()
        with self.assertRaises(CacheError):
            store.get("anything")
        with self.assertRaises(CacheError):
            store.put("anything", {})
        with self.assertRaises(CacheError):
            with store.lock("anything"):
                self.fail("Closed store entered a lock")


if __name__ == "__main__":
    unittest.main()
