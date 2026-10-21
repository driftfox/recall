"""Tests for opencode support: DB resolution, parser, indexing, and reader.

opencode stores every session in one SQLite database
(``~/.local/share/opencode/opencode.db``, ``%LOCALAPPDATA%\\opencode\\Data`` on
some Windows builds, or ``~/Library/Application Support/opencode`` on macOS)
rather than a file per session. A turn is a ``message`` row whose role lives in
a JSON ``data`` blob, with the content split across ``part`` rows of typed
chunks; only ``text`` parts carry conversational content, and parts flagged
``synthetic`` are harness context. Fixtures are built as a SQLite file inside
tmpdir -- no fixture files are committed.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import recall  # noqa: E402
import read_session  # noqa: E402


@contextmanager
def _no_opencode_env(extra=None):
    """Patch os.environ, dropping any ambient opencode overrides.

    The full environment is rebuilt without ``OPENCODE_DB`` / ``OPENCODE_DATA_DIR``
    so a developer's ambient settings can't leak into a test, then any explicit
    ``extra`` values are layered on top (so a test can *set* those overrides).
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENCODE_DB", "OPENCODE_DATA_DIR")}
    env.update(extra or {})
    with mock.patch.dict(os.environ, env, clear=True):
        yield


class TestOpencodeDataDir(unittest.TestCase):
    def test_linux_uses_xdg_data_home(self):
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/xdg"}):
            self.assertEqual(recall.opencode_data_dir(), Path("/xdg") / "opencode")

    def test_macos_uses_application_support(self):
        with mock.patch.object(sys, "platform", "darwin"), \
             _no_opencode_env({"XDG_DATA_HOME": "/no-such-xdg"}):
            self.assertEqual(
                recall.opencode_data_dir(),
                Path.home() / "Library" / "Application Support" / "opencode",
            )

    def test_windows_candidates_include_localappdata(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sys, "platform", "win32"), \
                 _no_opencode_env({"LOCALAPPDATA": d, "XDG_DATA_HOME": str(Path(d) / "no-xdg")}):
                dirs = recall.opencode_data_dirs()
                self.assertIn(Path(d) / "opencode" / "Data", dirs)
                self.assertIn(Path(d) / "opencode", dirs)

    def test_windows_prefers_the_localappdata_data_dir_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            data_dir = Path(d) / "opencode" / "Data"
            data_dir.mkdir(parents=True)
            with mock.patch.object(sys, "platform", "win32"), \
                 _no_opencode_env({"LOCALAPPDATA": d, "XDG_DATA_HOME": str(Path(d) / "no-xdg")}):
                self.assertEqual(recall.opencode_data_dir(), data_dir)

    def test_opencode_data_dir_env_override_wins(self):
        with tempfile.TemporaryDirectory() as d:
            with _no_opencode_env({"OPENCODE_DATA_DIR": d}):
                self.assertEqual(recall.opencode_data_dir(), Path(d))
                self.assertEqual(recall.opencode_data_dirs(), [Path(d)])


class TestResolveOpencodeDb(unittest.TestCase):
    def test_absolute_override_wins(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "custom.db"
            with _no_opencode_env({"OPENCODE_DB": str(target)}):
                self.assertEqual(recall.resolve_opencode_db(), target)

    def test_bare_filename_resolves_inside_data_dir(self):
        with mock.patch.object(sys, "platform", "linux"), \
             _no_opencode_env({"OPENCODE_DB": "custom.db", "XDG_DATA_HOME": "/xdg"}):
            self.assertEqual(recall.resolve_opencode_db(), Path("/xdg/opencode/custom.db"))

    def test_bare_filename_resolves_inside_opencode_data_dir_override(self):
        with tempfile.TemporaryDirectory() as d:
            with _no_opencode_env({"OPENCODE_DB": "custom.db", "OPENCODE_DATA_DIR": d}):
                self.assertEqual(recall.resolve_opencode_db(), Path(d) / "custom.db")

    def test_memory_override_is_none(self):
        with _no_opencode_env({"OPENCODE_DB": ":memory:"}):
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

    def test_stable_db_wins_over_channel_db_anywhere(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "opencode").mkdir()
            (Path(d) / "opencode" / "opencode.db").write_text("")
            (Path(d) / "opencode" / "opencode-dev.db").write_text("")
            with mock.patch.object(sys, "platform", "linux"), _no_opencode_env({"XDG_DATA_HOME": d}):
                self.assertEqual(
                    recall.resolve_opencode_db(), Path(d) / "opencode" / "opencode.db"
                )

    def test_missing_db_returns_default_path(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sys, "platform", "linux"), _no_opencode_env({"XDG_DATA_HOME": d}):
                self.assertEqual(
                    recall.resolve_opencode_db(), Path(d) / "opencode" / "opencode.db"
                )

    def test_opencode_db_global_overrides_discovery(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "explicit.db"
            self.addCleanup(setattr, recall, "OPENCODE_DB", recall.OPENCODE_DB)
            recall.OPENCODE_DB = target
            self.assertEqual(recall.opencode_db(), target)


OPENCODE_SCHEMA = """
    CREATE TABLE session (
        id TEXT PRIMARY KEY,
        project_id TEXT,
        workspace_id TEXT,
        parent_id TEXT,
        slug TEXT,
        directory TEXT,
        path TEXT,
        title TEXT,
        time_created INTEGER,
        time_updated INTEGER
    );
    CREATE TABLE message (
        id TEXT PRIMARY KEY,
        session_id TEXT,
        time_created INTEGER,
        data TEXT
    );
    CREATE TABLE part (
        id TEXT PRIMARY KEY,
        message_id TEXT,
        session_id TEXT,
        time_created INTEGER,
        data TEXT
    );
"""

# Older databases predate the ``slug`` column; the parser must fall back.
OPENCODE_SCHEMA_NO_SLUG = """
    CREATE TABLE session (
        id TEXT PRIMARY KEY,
        parent_id TEXT,
        title TEXT,
        directory TEXT,
        time_created INTEGER,
        time_updated INTEGER
    );
    CREATE TABLE message (
        id TEXT PRIMARY KEY,
        session_id TEXT,
        time_created INTEGER,
        data TEXT
    );
    CREATE TABLE part (
        id TEXT PRIMARY KEY,
        message_id TEXT,
        session_id TEXT,
        time_created INTEGER,
        data TEXT
    );
"""


def make_opencode_db(path, sessions, schema=OPENCODE_SCHEMA):
    """Create a minimal opencode-shaped SQLite DB.

    ``sessions`` is a list of dicts:
      {"id", "slug", "title", "directory", "parent_id", "created", "updated",
       "messages": [{"id", "role", "parts": [{"type", "text"}, ...]}, ...]}

    A part may carry ``synthetic: True`` or a raw ``data`` override (for
    malformed-JSON cases).
    """
    has_slug = "slug" in schema
    conn = sqlite3.connect(str(path))
    conn.executescript(schema)
    for s in sessions:
        created = s["created"]
        updated = s.get("updated", created)
        if has_slug:
            conn.execute(
                "INSERT INTO session (id, parent_id, slug, title, directory, "
                "time_created, time_updated) VALUES (?,?,?,?,?,?,?)",
                (s["id"], s.get("parent_id"), s.get("slug"), s.get("title", ""),
                 s.get("directory", ""), created, updated),
            )
        else:
            conn.execute(
                "INSERT INTO session (id, parent_id, title, directory, "
                "time_created, time_updated) VALUES (?,?,?,?,?,?)",
                (s["id"], s.get("parent_id"), s.get("title", ""),
                 s.get("directory", ""), created, updated),
            )
        for i, m in enumerate(s["messages"]):
            conn.execute(
                "INSERT INTO message VALUES (?,?,?,?)",
                (m["id"], s["id"], created + i, json.dumps({"role": m["role"]})),
            )
            for j, p in enumerate(m["parts"]):
                if "data" in p:
                    data = p["data"]
                else:
                    data = json.dumps({k: v for k, v in p.items()})
                conn.execute(
                    "INSERT INTO part VALUES (?,?,?,?,?)",
                    (p.get("id", f"{m['id']}-p{j}"), m["id"], s["id"], created + i, data),
                )
    conn.commit()
    return conn


SAMPLE_SESSION = {
    "id": "ses_abc123",
    "slug": "shiny-star",
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


class OpencodeFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "opencode.db"
        make_opencode_db(self.db, [SAMPLE_SESSION])

    def connect(self):
        conn = sqlite3.connect(str(self.db))
        self.addCleanup(conn.close)
        return conn


class TestOpencodeSessionMessages(OpencodeFixture):
    def test_keeps_only_user_and_assistant_text(self):
        conn = self.connect()
        self.assertEqual(recall.opencode_session_messages(conn, "ses_abc123"), [
            ("user", "hello needle"),
            ("assistant", "working on it"),
        ])

    def test_skips_synthetic_text_parts(self):
        s = {"id": "ses_syn", "slug": "s", "title": "t", "directory": "/w",
             "created": 1, "updated": 1, "messages": [
                 {"id": "m1", "role": "user", "parts": [
                     {"type": "text", "text": "real question"},
                     {"type": "text", "text": "injected reminder", "synthetic": True}]}]}
        db = str(self.root / "syn.db")
        make_opencode_db(db, [s])
        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        self.assertEqual(recall.opencode_session_messages(conn, "ses_syn"),
                         [("user", "real question")])

    def test_skips_non_text_parts_even_when_they_carry_text(self):
        conn = self.connect()
        roles_and_text = recall.opencode_session_messages(conn, "ses_abc123")
        self.assertNotIn("thinking", " ".join(t for _, t in roles_and_text))
        self.assertNotIn("tool output", " ".join(t for _, t in roles_and_text))

    def test_joins_multiple_text_parts_in_order(self):
        s = {"id": "ses_join", "slug": "s", "title": "t", "directory": "/w",
             "created": 1, "updated": 1, "messages": [
                 {"id": "m1", "role": "assistant", "parts": [
                     {"type": "text", "text": "first"},
                     {"type": "text", "text": "second"}]}]}
        db = str(self.root / "join.db")
        make_opencode_db(db, [s])
        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        self.assertEqual(recall.opencode_session_messages(conn, "ses_join"),
                         [("assistant", "first\nsecond")])

    def test_skips_messages_with_non_conversational_roles(self):
        s = {"id": "ses_roles", "slug": "s", "title": "t", "directory": "/w",
             "created": 1, "updated": 1, "messages": [
                 {"id": "m1", "role": "system", "parts": [
                     {"type": "text", "text": "system prompt"}]},
                 {"id": "m2", "role": "user", "parts": [
                     {"type": "text", "text": "hello"}]}]}
        db = str(self.root / "roles.db")
        make_opencode_db(db, [s])
        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        self.assertEqual(recall.opencode_session_messages(conn, "ses_roles"),
                         [("user", "hello")])

    def test_a_corrupt_part_costs_only_that_part(self):
        s = {"id": "ses_bad", "slug": "s", "title": "t", "directory": "/w",
             "created": 1, "updated": 1, "messages": [
                 {"id": "m1", "role": "user", "parts": [
                     {"type": "text", "text": "survives"},
                     {"type": "text", "text": "junk", "data": "{not json"}]}]}
        db = str(self.root / "bad.db")
        make_opencode_db(db, [s])
        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        self.assertEqual(recall.opencode_session_messages(conn, "ses_bad"),
                         [("user", "survives")])

    def test_a_non_object_part_is_skipped(self):
        s = {"id": "ses_nonobj", "slug": "s", "title": "t", "directory": "/w",
             "created": 1, "updated": 1, "messages": [
                 {"id": "m1", "role": "user", "parts": [
                     {"type": "text", "text": "kept"},
                     {"type": "text", "text": "junk", "data": "[1, 2, 3]"}]}]}
        db = str(self.root / "nonobj.db")
        make_opencode_db(db, [s])
        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        self.assertEqual(recall.opencode_session_messages(conn, "ses_nonobj"),
                         [("user", "kept")])

    def test_an_empty_session_returns_nothing(self):
        s = {"id": "ses_empty", "slug": "s", "title": "t", "directory": "/w",
             "created": 1, "updated": 1, "messages": []}
        db = str(self.root / "empty.db")
        make_opencode_db(db, [s])
        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        self.assertEqual(recall.opencode_session_messages(conn, "ses_empty"), [])


class TestParseOpencodeSessions(OpencodeFixture):
    def test_extracts_only_user_and_assistant_text_parts(self):
        (metadata, messages), = list(recall.parse_opencode_sessions(str(self.db)))
        self.assertEqual([role for role, _ in messages], ["user", "assistant"])
        self.assertIn("needle", messages[0][1])
        self.assertIn("working on it", messages[1][1])
        self.assertNotIn("thinking", messages[1][1])
        self.assertNotIn("tool output", messages[1][1])

    def test_metadata_shape(self):
        (metadata, _), = list(recall.parse_opencode_sessions(str(self.db)))
        self.assertEqual(metadata["session_id"], "ses_abc123")
        self.assertEqual(metadata["source"], "opencode")
        self.assertEqual(metadata["project"], "/work/proj")
        self.assertEqual(metadata["slug"], "shiny-star")
        self.assertEqual(metadata["timestamp"], 1_700_000_000_000)
        self.assertEqual(metadata["file_path"], f"{self.db}#ses_abc123")
        self.assertEqual(metadata["mtime"], 1_700_000_500.0)

    def test_slug_falls_back_to_title_when_null(self):
        s = dict(SAMPLE_SESSION, id="ses_titled", slug=None, title="Titled")
        db = str(self.root / "titled.db")
        make_opencode_db(db, [s])
        (metadata, _), = list(recall.parse_opencode_sessions(db))
        self.assertEqual(metadata["slug"], "Titled")

    def test_slug_falls_back_to_id_when_title_empty(self):
        s = dict(SAMPLE_SESSION, id="ses_notitle", slug=None, title="")
        db = str(self.root / "notitle.db")
        make_opencode_db(db, [s])
        (metadata, _), = list(recall.parse_opencode_sessions(db))
        self.assertEqual(metadata["slug"], "ses_notitle")

    def test_reads_databases_without_a_slug_column(self):
        db = str(self.root / "legacy.db")
        make_opencode_db(db, [SAMPLE_SESSION], schema=OPENCODE_SCHEMA_NO_SLUG)
        (metadata, messages), = list(recall.parse_opencode_sessions(db))
        self.assertEqual(metadata["slug"], "fixing the widget")
        self.assertEqual([role for role, _ in messages], ["user", "assistant"])

    def test_session_with_no_text_parts_is_still_yielded(self):
        s = {"id": "ses_empty", "slug": "s", "title": "t", "directory": "/w",
             "created": 1, "updated": 1, "messages": [
                 {"id": "m", "role": "assistant", "parts": [
                     {"type": "step-start", "text": ""}]}]}
        db = str(self.root / "empty.db")
        make_opencode_db(db, [s])
        (metadata, messages), = list(recall.parse_opencode_sessions(db))
        self.assertEqual(messages, [])

    def test_reads_live_wal_database_while_a_writer_holds_it(self):
        db = str(self.root / "wal.db")
        writer = make_opencode_db(db, [SAMPLE_SESSION])
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO session (id, slug, title, directory, time_created, time_updated) "
                       "VALUES ('ses_live',NULL,'t','/w',1,2)")
        writer.commit()  # keep `writer` open -- the -wal file stays on disk
        self.addCleanup(writer.close)
        ids = [md["session_id"] for md, _ in recall.parse_opencode_sessions(db)]
        self.assertIn("ses_live", ids)


class TestIndexingOpencode(OpencodeFixture):
    def setUp(self):
        super().setUp()
        for name, value in (("CLAUDE_PROJECTS_DIR", self.root / "none"),
                            ("CODEX_SESSIONS_DIR", self.root / "none"),
                            ("PI_SESSIONS_DIR", self.root / "none"),
                            ("GROK_SESSIONS_DIR", self.root / "none"),
                            ("CURSOR_PROJECTS_DIR", self.root / "none"),
                            ("OMP_SESSIONS_DIR", self.root / "none"),
                            ("OPENCODE_DB", Path(self.db))):
            self.addCleanup(setattr, recall, name, getattr(recall, name))
            setattr(recall, name, value)

    def indexed(self, force=False):
        conn = sqlite3.connect(str(self.root / "index.db"))
        self.addCleanup(conn.close)
        recall.create_schema(conn)
        recall.migrate_schema(conn)
        result = recall.index_sessions(conn, force=force)
        conn.commit()
        return conn, result

    def test_indexes_and_searches_opencode(self):
        conn, _ = self.indexed()
        results = recall.search(conn, "needle")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1], "opencode")
        self.assertEqual(results[0][0], "ses_abc123")

    def test_indexes_a_session_with_its_metadata(self):
        conn, _ = self.indexed()
        row = conn.execute(
            "SELECT source, file_path, project, slug, timestamp FROM sessions WHERE session_id = ?",
            ("ses_abc123",)).fetchone()
        self.assertEqual(row, ("opencode", f"{self.db}#ses_abc123", "/work/proj",
                               "shiny-star", 1_700_000_000_000))

    def test_source_filter(self):
        self.indexed()
        conn = sqlite3.connect(str(self.root / "index.db"))
        self.addCleanup(conn.close)
        rows = recall.list_sessions(conn, source="opencode")
        self.assertEqual([r[0] for r in rows], ["ses_abc123"])

    def test_unchanged_session_is_skipped_on_second_run(self):
        self.indexed()
        conn = sqlite3.connect(str(self.root / "index.db"))
        self.addCleanup(conn.close)
        _, skipped, *_ = recall.index_sessions(conn)
        self.assertGreaterEqual(skipped, 1)

    def test_force_reindex_rebuilds(self):
        self.indexed()
        conn = sqlite3.connect(str(self.root / "index.db"))
        self.addCleanup(conn.close)
        indexed, _, *_ = recall.index_sessions(conn, force=True)
        self.assertEqual(indexed, 1)

    def test_missing_db_is_skipped_without_error(self):
        recall.OPENCODE_DB = self.root / "does-not-exist.db"
        conn = sqlite3.connect(str(self.root / "index.db"))
        self.addCleanup(conn.close)
        recall.create_schema(conn)
        recall.migrate_schema(conn)
        indexed, skipped, *_ = recall.index_sessions(conn)
        self.assertEqual(indexed, 0)

    def test_slug_falls_back_to_title_then_id(self):
        s = dict(SAMPLE_SESSION, id="ses_titled", slug=None, title="Titled")
        make_opencode_db(str(self.root / "second.db"), [s])
        recall.OPENCODE_DB = self.root / "second.db"
        conn, _ = self.indexed()
        self.assertEqual(conn.execute(
            "SELECT slug FROM sessions WHERE session_id = 'ses_titled'").fetchone()[0],
            "Titled")

    def test_subagent_sessions_are_indexed_too(self):
        parent = dict(SAMPLE_SESSION, id="ses_parent", slug="p")
        child = {"id": "ses_child", "slug": "c", "title": "t", "directory": "/w",
                 "parent_id": "ses_parent", "created": 1, "updated": 1, "messages": [
                     {"id": "m1", "role": "user", "parts": [
                         {"type": "text", "text": "subagent work"}]}]}
        recall.OPENCODE_DB = self.root / "parentchild.db"
        make_opencode_db(recall.OPENCODE_DB, [parent, child])
        conn, _ = self.indexed()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id = 'ses_child'").fetchone()[0], 1)

    def test_changed_session_is_reindexed_without_duplicates(self):
        s = dict(SAMPLE_SESSION, id="ses_a", updated=1000,
                 messages=[{"id": "m1", "role": "user", "parts": [
                     {"type": "text", "text": "old text"}]}])
        recall.OPENCODE_DB = self.root / "changed.db"
        make_opencode_db(recall.OPENCODE_DB, [s])
        conn, _ = self.indexed()
        conn.close()

        writer = sqlite3.connect(str(recall.OPENCODE_DB))
        writer.execute("UPDATE session SET time_updated = 2000 WHERE id = 'ses_a'")
        writer.execute("DELETE FROM part")
        writer.execute(
            "INSERT INTO part VALUES (?,?,?,?,?)",
            ("m1-p0", "m1", "ses_a", 1, json.dumps({"type": "text", "text": "new text"})))
        writer.commit()
        writer.close()

        conn = sqlite3.connect(str(self.root / "index.db"))
        self.addCleanup(conn.close)
        recall.index_sessions(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(recall.search(conn, "old", limit=10), [])
        self.assertEqual([h[0] for h in recall.search(conn, "new", limit=10)], ["ses_a"])

    def test_env_var_selects_the_database(self):
        recall.OPENCODE_DB = None
        self.addCleanup(os.environ.pop, "OPENCODE_DB", None)
        os.environ["OPENCODE_DB"] = str(self.db)
        conn, (indexed, *_rest) = self.indexed()
        self.assertGreaterEqual(indexed, 1)


class TestReadSessionOpencode(OpencodeFixture):
    def setUp(self):
        super().setUp()
        self.ref = f"{self.db}#ses_abc123"

    def test_detects_opencode_reference(self):
        self.assertEqual(read_session.detect_format(self.ref), "opencode")

    def test_reads_user_and_assistant_text(self):
        msgs = list(read_session.iter_messages(self.ref))
        self.assertEqual([role for role, _ in msgs], ["user", "assistant"])
        self.assertIn("needle", msgs[0][1])

    def test_a_bare_session_id_is_recognised(self):
        self.assertTrue(read_session.looks_like_opencode("ses_abc123"))
        self.assertFalse(read_session.looks_like_opencode("/tmp/session.jsonl"))

    def test_a_db_hash_reference_is_recognised(self):
        self.assertTrue(read_session.looks_like_opencode(self.ref))

    def test_a_path_to_a_transcript_is_not_opencode(self):
        self.assertFalse(read_session.looks_like_opencode(str(self.root / "chat_history.jsonl")))

    def test_a_bare_session_id_reads_through_the_default_database(self):
        self.addCleanup(setattr, recall, "OPENCODE_DB", recall.OPENCODE_DB)
        recall.OPENCODE_DB = self.db
        self.assertEqual(list(read_session.iter_opencode_messages("ses_abc123")),
                         [("user", "hello needle"), ("assistant", "working on it")])

    def test_a_db_hash_reference_reads_that_database(self):
        self.assertEqual(list(read_session.iter_opencode_messages(self.ref)),
                         [("user", "hello needle"), ("assistant", "working on it")])

    def test_db_path_with_spaces_and_hash_in_name(self):
        spaced = str(self.root / "Application Support#v2" / "opencode.db")
        Path(spaced).parent.mkdir(parents=True, exist_ok=True)
        make_opencode_db(spaced, [SAMPLE_SESSION])
        ref = f"{spaced}#ses_abc123"
        self.assertEqual(read_session.detect_format(ref), "opencode")
        self.assertEqual(len(list(read_session.iter_messages(ref))), 2)

    def test_missing_db_reference_yields_no_messages(self):
        ref = f"{self.root}/does-not-exist.db#ses_abc123"
        self.assertEqual(read_session.detect_format(ref), "opencode")
        self.assertEqual(list(read_session.iter_messages(ref)), [])


class TestOpenOpencodeDb(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_missing_db_raises_without_creating_file(self):
        missing = self.root / "nope.db"
        with self.assertRaises(sqlite3.OperationalError):
            recall._open_opencode_db(missing)
        self.assertFalse(missing.exists())

    def test_connection_rejects_writes(self):
        db = str(self.root / "opencode.db")
        make_opencode_db(db, [SAMPLE_SESSION])
        conn = recall._open_opencode_db(db)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO session (id, title, directory, time_created, time_updated) "
                         "VALUES ('x','t','/w',1,2)")


REAL_OPENCODE_DB = recall.resolve_opencode_db()


@unittest.skipUnless(
    REAL_OPENCODE_DB and Path(REAL_OPENCODE_DB).is_file(),
    "no real opencode database on this host",
)
class TestRealOpencodeDatabase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name, value in (("CLAUDE_PROJECTS_DIR", self.root / "none"),
                            ("CODEX_SESSIONS_DIR", self.root / "none"),
                            ("PI_SESSIONS_DIR", self.root / "none"),
                            ("GROK_SESSIONS_DIR", self.root / "none"),
                            ("CURSOR_PROJECTS_DIR", self.root / "none"),
                            ("OPENCODE_DB", REAL_OPENCODE_DB)):
            self.addCleanup(setattr, recall, name, getattr(recall, name))
            setattr(recall, name, value)

    def test_indexes_real_database(self):
        conn = sqlite3.connect(str(self.root / "index.db"))
        self.addCleanup(conn.close)
        recall.create_schema(conn)
        recall.migrate_schema(conn)
        indexed, *_ = recall.index_sessions(conn)
        self.assertGreaterEqual(indexed, 1)


if __name__ == "__main__":
    unittest.main()
