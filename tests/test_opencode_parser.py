"""Tests for opencode support: DB resolution, parser, indexing, and reader."""
from __future__ import annotations

import json
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


def make_opencode_db(path, sessions):
    """Create a minimal opencode-shaped SQLite DB.

    ``sessions`` is a list of dicts:
      {"id", "title", "directory", "created", "updated",
       "messages": [{"id", "role", "parts": [{"type", "text"}, ...]}, ...]}
    """
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, title TEXT,
            directory TEXT, time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,
            time_created INTEGER, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, data TEXT);
    """)
    for s in sessions:
        conn.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?)",
            (s["id"], s.get("parent_id"), s.get("title", ""),
             s.get("directory", ""), s["created"], s["updated"]),
        )
        for i, m in enumerate(s["messages"]):
            conn.execute(
                "INSERT INTO message VALUES (?,?,?,?)",
                (m["id"], s["id"], s["created"] + i,
                 json.dumps({"role": m["role"]})),
            )
            for j, p in enumerate(m["parts"]):
                conn.execute(
                    "INSERT INTO part (id, message_id, session_id, time_created, data)"
                    " VALUES (?,?,?,?,?)",
                    (p.get("id", f"{m['id']}_p{j}"), m["id"], s["id"],
                     s["created"] + i, p.get("data", json.dumps(
                         {"type": p["type"], "text": p.get("text", "")}))),
                )
    conn.commit()
    return conn


SAMPLE_SESSION = {
    "id": "ses_abc123",
    "title": "fixing the widget",
    "directory": "/work/proj",
    "created": 1_700_000_000_000,
    "updated": 1_700_000_500_000,
    "messages": [
        {"id": "msg_1", "role": "user", "parts": [
            {"type": "text", "text": "hello needle"}]},
        {"id": "msg_2", "role": "assistant", "parts": [
            {"type": "reasoning", "text": "thinking..."},
            {"type": "text", "text": "working on it"},
            {"type": "tool", "text": "tool output"},
            {"type": "step-start", "text": ""}]},
    ],
}


class TestParseOpencodeSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "opencode.db")
        make_opencode_db(self.db, [SAMPLE_SESSION])

    def test_extracts_only_user_and_assistant_text_parts(self):
        (metadata, messages), = list(recall.parse_opencode_sessions(self.db))
        self.assertEqual([role for role, _ in messages], ["user", "assistant"])
        self.assertIn("needle", messages[0][1])
        self.assertIn("working on it", messages[1][1])
        self.assertNotIn("thinking", messages[1][1])
        self.assertNotIn("tool output", messages[1][1])

    def test_metadata_shape(self):
        (metadata, _), = list(recall.parse_opencode_sessions(self.db))
        self.assertEqual(metadata["session_id"], "ses_abc123")
        self.assertEqual(metadata["source"], "opencode")
        self.assertEqual(metadata["project"], "/work/proj")
        self.assertEqual(metadata["slug"], "fixing the widget")
        self.assertEqual(metadata["timestamp"], 1_700_000_000_000)
        self.assertEqual(metadata["file_path"], f"{self.db}#ses_abc123")
        self.assertEqual(metadata["mtime"], 1_700_000_500.0)

    def test_slug_falls_back_to_id_when_title_empty(self):
        s = dict(SAMPLE_SESSION, id="ses_noitle", title="")
        db = str(Path(self.tmp.name) / "empty_title.db")
        make_opencode_db(db, [s])
        (metadata, _), = list(recall.parse_opencode_sessions(db))
        self.assertEqual(metadata["slug"], "ses_noitle")

    def test_session_with_no_text_parts_is_still_yielded(self):
        s = {"id": "ses_empty", "title": "t", "directory": "/w",
             "created": 1, "updated": 1,
             "messages": [{"id": "m", "role": "assistant", "parts": [
                 {"type": "step-start", "text": ""}]}]}
        db = str(Path(self.tmp.name) / "empty.db")
        make_opencode_db(db, [s])
        (metadata, messages), = list(recall.parse_opencode_sessions(db))
        self.assertEqual(messages, [])

    def test_malformed_part_json_is_skipped(self):
        s = dict(SAMPLE_SESSION, id="ses_bad")
        s["messages"] = [
            {"id": "m1", "role": "user", "parts": [
                {"type": "text", "text": "ok", "data": "{not json"},
                {"type": "text", "text": "kept"}]},
        ]
        db = str(Path(self.tmp.name) / "bad.db")
        make_opencode_db(db, [s])
        _, messages = list(recall.parse_opencode_sessions(db))[0]
        self.assertEqual(messages, [("user", "kept")])

    def test_reads_live_wal_database_while_a_writer_holds_it(self):
        db = str(Path(self.tmp.name) / "wal.db")
        writer = make_opencode_db(db, [SAMPLE_SESSION])
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "INSERT INTO session VALUES ('ses_live',NULL,'t','/w',1,2)"
        )
        writer.commit()  # keep `writer` open — the -wal file stays on disk
        self.addCleanup(writer.close)
        ids = [md["session_id"] for md, _ in recall.parse_opencode_sessions(db)]
        self.assertIn("ses_live", ids)


class TestIndexingOpencode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = str(self.root / "opencode.db")
        make_opencode_db(self.db, [SAMPLE_SESSION])

        for name, value in (("CLAUDE_PROJECTS_DIR", self.root / "none"),
                            ("CODEX_SESSIONS_DIR", self.root / "none"),
                            ("PI_SESSIONS_DIR", self.root / "none"),
                            ("GROK_SESSIONS_DIR", self.root / "none"),
                            ("CURSOR_PROJECTS_DIR", self.root / "none"),
                            ("OPENCODE_DB", Path(self.db))):
            self.addCleanup(setattr, recall, name, getattr(recall, name))
            setattr(recall, name, value)

        self.index = sqlite3.connect(str(self.root / "index.db"))
        recall.create_schema(self.index)
        recall.migrate_schema(self.index)

    def test_indexes_and_searches_opencode(self):
        recall.index_sessions(self.index)
        results = recall.search(self.index, "needle")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1], "opencode")
        self.assertEqual(results[0][0], "ses_abc123")

    def test_source_filter(self):
        recall.index_sessions(self.index)
        rows = recall.list_sessions(self.index, source="opencode")
        self.assertEqual([r[0] for r in rows], ["ses_abc123"])

    def test_unchanged_session_is_skipped_on_second_run(self):
        recall.index_sessions(self.index)
        _, skipped, *_ = recall.index_sessions(self.index)
        self.assertGreaterEqual(skipped, 1)

    def test_force_reindex_rebuilds(self):
        recall.index_sessions(self.index)
        indexed, _, *_ = recall.index_sessions(self.index, force=True)
        self.assertEqual(indexed, 1)

    def test_missing_db_is_skipped_without_error(self):
        recall.OPENCODE_DB = self.root / "does-not-exist.db"
        indexed, skipped, *_ = recall.index_sessions(self.index)
        self.assertEqual(indexed, 0)


class TestReadSessionOpencode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "opencode.db")
        make_opencode_db(self.db, [SAMPLE_SESSION])
        self.ref = f"{self.db}#ses_abc123"

    def test_detects_opencode_reference(self):
        self.assertEqual(read_session.detect_format(self.ref), "opencode")

    def test_reads_user_and_assistant_text(self):
        msgs = list(read_session.iter_messages(self.ref))
        self.assertEqual([role for role, _ in msgs], ["user", "assistant"])
        self.assertIn("needle", msgs[0][1])

    def test_db_path_with_spaces_and_hash_in_name(self):
        spaced = self.tmp.name + "/Application Support/opencode.db"
        Path(spaced).parent.mkdir(parents=True, exist_ok=True)
        make_opencode_db(spaced, [SAMPLE_SESSION])
        ref = f"{spaced}#ses_abc123"
        self.assertEqual(read_session.detect_format(ref), "opencode")
        self.assertEqual(len(list(read_session.iter_messages(ref))), 2)
