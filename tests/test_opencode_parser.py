"""Tests for opencode session reading and indexing.

opencode stores every session in one SQLite database
(~/.local/share/opencode/opencode.db) rather than a file per session. A turn
is a `message` row whose role lives in a JSON `data` blob, with the content
split across `part` rows of typed chunks; only `text` parts carry
conversational content, and parts flagged `synthetic` are harness context.

Fixtures are built as an in-memory-shaped SQLite file inside tmpdir — no
fixture files committed.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Make scripts/ importable as a module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import recall  # noqa: E402
import read_session  # noqa: E402


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


class OpenCodeFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "opencode.db"
        self.addCleanup(os.environ.pop, "OPENCODE_DB", None)
        os.environ["OPENCODE_DB"] = str(self.db)

        conn = sqlite3.connect(str(self.db))
        conn.executescript(OPENCODE_SCHEMA)
        conn.commit()
        conn.close()

    def connect(self):
        conn = sqlite3.connect(str(self.db))
        self.addCleanup(conn.close)
        return conn

    def add_session(self, sid, directory="/home/u/project", slug=None, title=None,
                    created=1_700_000_000_000, updated=None, parent_id=None):
        conn = self.connect()
        conn.execute(
            "INSERT INTO session (id, directory, slug, title, parent_id, time_created, time_updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, directory, slug, title, parent_id, created, updated if updated is not None else created),
        )
        conn.commit()
        return conn

    def add_turn(self, sid, mid, role, parts, created=1_700_000_000_000):
        """Add one message and its parts. `parts` is a list of part dicts."""
        conn = self.connect()
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            (mid, sid, created, json.dumps({"role": role})),
        )
        for i, part in enumerate(parts):
            conn.execute(
                "INSERT INTO part (id, message_id, session_id, time_created, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"{mid}-p{i}", mid, sid, created + i, json.dumps(part)),
            )
        conn.commit()


class ParseOpenCodeSession(OpenCodeFixture):
    def test_keeps_only_user_and_assistant_text(self):
        self.add_session("ses_a")
        self.add_turn("ses_a", "m1", "user", [{"type": "text", "text": "why does the build fail"}])
        self.add_turn("ses_a", "m2", "assistant", [{"type": "text", "text": "the toolchain is pinned"}])
        conn = self.connect()
        self.assertEqual(recall.opencode_session_messages(conn, "ses_a"), [
            ("user", "why does the build fail"),
            ("assistant", "the toolchain is pinned"),
        ])

    def test_skips_synthetic_text_parts(self):
        self.add_session("ses_a")
        self.add_turn("ses_a", "m1", "user", [
            {"type": "text", "text": "real question"},
            {"type": "text", "text": "injected reminder", "synthetic": True},
        ])
        conn = self.connect()
        self.assertEqual(recall.opencode_session_messages(conn, "ses_a"),
                         [("user", "real question")])

    def test_skips_non_text_parts_even_when_they_carry_text(self):
        # reasoning parts also have a "text" key; only type == "text" counts.
        self.add_session("ses_a")
        self.add_turn("ses_a", "m1", "assistant", [
            {"type": "reasoning", "text": "considering the toolchain"},
            {"type": "tool", "tool": "bash"},
            {"type": "step-start"},
            {"type": "text", "text": "the answer"},
        ])
        conn = self.connect()
        self.assertEqual(recall.opencode_session_messages(conn, "ses_a"),
                         [("assistant", "the answer")])

    def test_joins_multiple_text_parts_in_order(self):
        self.add_session("ses_a")
        self.add_turn("ses_a", "m1", "assistant", [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ])
        conn = self.connect()
        self.assertEqual(recall.opencode_session_messages(conn, "ses_a"),
                         [("assistant", "first\nsecond")])

    def test_skips_messages_with_non_conversational_roles(self):
        self.add_session("ses_a")
        self.add_turn("ses_a", "m1", "system", [{"type": "text", "text": "system prompt"}])
        self.add_turn("ses_a", "m2", "user", [{"type": "text", "text": "hello"}])
        conn = self.connect()
        self.assertEqual(recall.opencode_session_messages(conn, "ses_a"), [("user", "hello")])

    def test_a_corrupt_part_costs_only_that_part(self):
        self.add_session("ses_a")
        self.add_turn("ses_a", "m1", "user", [{"type": "text", "text": "survives"}])
        conn = self.connect()
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
            ("bad", "m1", "ses_a", 1_700_000_000_001, "{not json"),
        )
        conn.commit()
        self.assertEqual(recall.opencode_session_messages(conn, "ses_a"), [("user", "survives")])

    def test_an_empty_session_returns_nothing(self):
        self.add_session("ses_a")
        conn = self.connect()
        self.assertEqual(recall.opencode_session_messages(conn, "ses_a"), [])


class IndexOpenCodeSessions(OpenCodeFixture):
    def setUp(self):
        super().setUp()
        # Keep the file-based sources out of the way.
        for name in ("CLAUDE_PROJECTS_DIR", "CODEX_SESSIONS_DIR",
                     "PI_SESSIONS_DIR", "GROK_SESSIONS_DIR"):
            self.addCleanup(setattr, recall, name, getattr(recall, name))
            setattr(recall, name, self.root / "none")

        # Keep this run off the host's real omp sessions.
        self.addCleanup(os.environ.pop, "OMP_SESSION_DIR", None)
        os.environ["OMP_SESSION_DIR"] = str(self.root / "none")

    def indexed(self, force=False):
        conn = sqlite3.connect(str(self.root / "index.db"))
        recall.create_schema(conn)
        recall.migrate_schema(conn)
        result = recall.index_sessions(conn, force=force)
        conn.commit()
        return conn, result

    def test_indexes_a_session_with_its_metadata(self):
        self.add_session("ses_a", directory="/home/u/proj", slug="proj-slug",
                         title="A title", created=1_700_000_000_000)
        self.add_turn("ses_a", "m1", "user", [{"type": "text", "text": "buffer store question"}])
        conn, _ = self.indexed()
        row = conn.execute(
            "SELECT source, file_path, project, slug, timestamp FROM sessions WHERE session_id = ?",
            ("ses_a",)).fetchone()
        self.assertEqual(row, ("opencode", f"{self.db}#ses_a", "/home/u/proj",
                               "proj-slug", 1_700_000_000_000))

    def test_slug_falls_back_to_title_then_id(self):
        self.add_session("ses_a", title="Titled", slug=None)
        self.add_turn("ses_a", "m1", "user", [{"type": "text", "text": "x"}])
        self.add_session("ses_b", title=None, slug=None)
        self.add_turn("ses_b", "m2", "user", [{"type": "text", "text": "y"}])
        conn, _ = self.indexed()
        self.assertEqual(conn.execute(
            "SELECT slug FROM sessions WHERE session_id = 'ses_a'").fetchone()[0], "Titled")
        self.assertEqual(conn.execute(
            "SELECT slug FROM sessions WHERE session_id = 'ses_b'").fetchone()[0], "ses_b")

    def test_indexed_text_is_searchable(self):
        self.add_session("ses_a")
        self.add_turn("ses_a", "m1", "user", [{"type": "text", "text": "the opencode probe"}])
        conn, _ = self.indexed()
        hits = recall.search(conn, "opencode", limit=10)
        self.assertEqual([h[0] for h in hits], ["ses_a"])
        self.assertEqual([h[1] for h in hits], ["opencode"])

    def test_subagent_sessions_are_indexed_too(self):
        self.add_session("ses_child", parent_id="ses_parent")
        self.add_turn("ses_child", "m1", "user", [{"type": "text", "text": "subagent work"}])
        conn, _ = self.indexed()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id = 'ses_child'").fetchone()[0], 1)

    def test_unchanged_sessions_are_skipped_on_the_next_run(self):
        self.add_session("ses_a")
        self.add_turn("ses_a", "m1", "user", [{"type": "text", "text": "hello"}])
        conn, _ = self.indexed()
        conn.close()
        conn, (indexed, _, _, _) = self.indexed()
        self.assertEqual(indexed, 0)

    def test_a_changed_session_is_reindexed_without_duplicates(self):
        self.add_session("ses_a", updated=1000)
        self.add_turn("ses_a", "m1", "user", [{"type": "text", "text": "old text"}])
        conn, _ = self.indexed()
        conn.close()

        # Same session, new content and a newer time_updated.
        conn = sqlite3.connect(str(self.db))
        conn.execute("UPDATE session SET time_updated = 2000 WHERE id = 'ses_a'")
        conn.execute("DELETE FROM part")
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
            ("m1-p0", "m1", "ses_a", 1, json.dumps({"type": "text", "text": "new text"})))
        conn.commit()
        conn.close()

        conn, _ = self.indexed()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(recall.search(conn, "old", limit=10), [])
        self.assertEqual([h[0] for h in recall.search(conn, "new", limit=10)], ["ses_a"])

    def test_a_missing_database_is_a_noop(self):
        os.environ["OPENCODE_DB"] = str(self.root / "absent.db")
        conn, (indexed, _, total_sessions, _) = self.indexed()
        self.assertEqual(indexed, 0)
        self.assertEqual(total_sessions, 0)


class ReadOpenCodeSession(OpenCodeFixture):
    def test_a_bare_session_id_is_recognised(self):
        self.assertTrue(read_session.looks_like_opencode("ses_abc123"))
        self.assertFalse(read_session.looks_like_opencode("/tmp/session.jsonl"))

    def test_a_db_hash_reference_is_recognised(self):
        ref = f"{self.db}#ses_abc"
        self.assertTrue(read_session.looks_like_opencode(ref))

    def test_a_bare_session_id_reads_through_the_default_database(self):
        self.add_session("ses_abc")
        self.add_turn("ses_abc", "m1", "user", [{"type": "text", "text": "recalled"}])
        self.assertEqual(list(read_session.iter_opencode_messages("ses_abc")),
                         [("user", "recalled")])

    def test_a_db_hash_reference_reads_that_database(self):
        self.add_session("ses_abc")
        self.add_turn("ses_abc", "m1", "assistant", [{"type": "text", "text": "answer"}])
        ref = f"{self.db}#ses_abc"
        self.assertEqual(list(read_session.iter_opencode_messages(ref)),
                         [("assistant", "answer")])

    def test_a_path_to_a_transcript_is_not_opencode(self):
        self.assertFalse(read_session.looks_like_opencode(str(self.root / "chat_history.jsonl")))


if __name__ == "__main__":
    unittest.main()
