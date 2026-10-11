"""Tests for opencode support: DB resolution, parser, indexing, and reader."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import recall  # noqa: E402
import read_session  # noqa: E402


def _no_opencode_env(extra=None):
    """Context manager: patch os.environ, always clearing OPENCODE_DB."""
    env = dict(extra or {})
    env.pop("OPENCODE_DB", None)
    return mock.patch.dict(os.environ, env, clear=False)


class TestOpencodeDataDir(unittest.TestCase):
    def test_linux_uses_xdg_data_home(self):
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/xdg"}):
            self.assertEqual(recall.opencode_data_dir(), Path("/xdg") / "opencode")

    def test_macos_uses_application_support(self):
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(
                recall.opencode_data_dir(),
                Path.home() / "Library" / "Application Support" / "opencode",
            )

    def test_windows_uses_localappdata_data(self):
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\a\AppData\Local"}):
            self.assertEqual(
                recall.opencode_data_dir(),
                Path(r"C:\Users\a\AppData\Local") / "opencode" / "Data",
            )


class TestResolveOpencodeDb(unittest.TestCase):
    def test_absolute_override_wins(self):
        with mock.patch.dict(os.environ, {"OPENCODE_DB": "/tmp/custom.db"}):
            self.assertEqual(recall.resolve_opencode_db(), Path("/tmp/custom.db"))

    def test_bare_filename_resolves_inside_data_dir(self):
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.dict(os.environ, {"OPENCODE_DB": "custom.db", "XDG_DATA_HOME": "/xdg"}):
            self.assertEqual(recall.resolve_opencode_db(), Path("/xdg/opencode/custom.db"))

    def test_memory_override_is_none(self):
        with mock.patch.dict(os.environ, {"OPENCODE_DB": ":memory:"}):
            self.assertIsNone(recall.resolve_opencode_db())

    def test_prefers_opencode_db(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "opencode").mkdir()
            (Path(d) / "opencode" / "opencode.db").write_text("")
            with mock.patch.object(sys, "platform", "linux"), _no_opencode_env({"XDG_DATA_HOME": d}):
                self.assertEqual(
                    recall.resolve_opencode_db(), Path(d) / "opencode" / "opencode.db"
                )

    def test_falls_back_to_newest_channel_db(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "opencode"
            base.mkdir()
            older = base / "opencode-beta.db"
            newer = base / "opencode-dev.db"
            older.write_text("")
            newer.write_text("")
            os.utime(older, (1_000, 1_000))
            os.utime(newer, (2_000, 2_000))
            with mock.patch.object(sys, "platform", "linux"), _no_opencode_env({"XDG_DATA_HOME": d}):
                self.assertEqual(recall.resolve_opencode_db(), newer)

    def test_missing_db_returns_default_path(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sys, "platform", "linux"), _no_opencode_env({"XDG_DATA_HOME": d}):
                self.assertEqual(
                    recall.resolve_opencode_db(), Path(d) / "opencode" / "opencode.db"
                )
