"""Tests for parse_omp_session and omp detection in read_session.

omp session format (oh-my-pi):
  - The file opens with a title slot: {"type": "title", "v": 1, "title": "..."}
  - Header line: {"type": "session", "version": 3, "id": "...", "timestamp": "...",
                  "cwd": "...", "title": "...", "titleSource": "auto"}
  - Turn lines: {"type": "message", "id": ..., "parentId": ..., "timestamp": ...,
                 "message": {"role": "user|assistant|toolResult", "content": [...]}}
  - Other top-level types we skip: custom, custom_message, model_change,
    thinking_level_change, title, title_change.
  - A session's subagents keep their own transcripts in the session's directory
    (<timestamp>_<uuid>/<AgentId>.jsonl); the advisor runtime writes
    __advisor*.jsonl there, which is not a session.

Fixtures are generated as strings inside tmpdir — no fixture files committed.
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


# ── Helpers ──────────────────────────────────────────────────────────────────


SESSION_NAME = "2026-10-21T22-26-10-177Z_01a16092-6881-7454-ab80-0d56728b7694"
SESSION_ID = "01a16092-6881-7454-ab80-0d56728b7694"


def write_jsonl(path: Path, entries: list[dict]) -> str:
    """Write a list of entries as JSONL and return the path as a string."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry))
            f.write("\n")
    return str(path)


def header(cwd="/home/u/project", session_id=SESSION_ID, title="Add omp support to recall",
           timestamp="2026-10-21T22:26:10.177Z") -> dict:
    """An omp session header record."""
    return {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
        "title": title,
        "titleSource": "auto",
    }


def turn(role, text, ident="m1", ts="2026-10-21T22:27:15.764Z", blocks=None) -> dict:
    """An omp message record with one text block."""
    content = blocks if blocks is not None else [{"type": "text", "text": text}]
    return {
        "type": "message",
        "id": ident,
        "parentId": None,
        "timestamp": ts,
        "message": {"role": role, "content": content},
    }


def session_file(root, entries=None, name=SESSION_NAME, cwd="/home/u/project",
                 session_id=SESSION_ID, title="Add omp support to recall",
                 timestamp="2026-10-21T22:26:10.177Z") -> str:
    """Write one omp session file under root and return its path."""
    records = [{"type": "title", "v": 1, "title": title, "source": "auto"}]
    records.append(header(cwd=cwd, session_id=session_id, title=title, timestamp=timestamp))
    records.extend(entries or [])
    return write_jsonl(Path(root) / name, records)


CONVERSATION = [
    turn("user", "add omp support to recall", ident="m1"),
    turn("assistant", "", ident="m2", blocks=[
        {"type": "thinking", "thinking": "The parser skips thinking blocks."},
        {"type": "text", "text": "omp sessions are a sixth source."},
        {"type": "toolCall", "id": "t1", "name": "bash", "arguments": {"cmd": "ls"}},
    ]),
    turn("toolResult", "file1\nfile2", ident="m3", blocks=[
        {"type": "text", "text": "file1\nfile2"},
    ]),
    {
        "type": "custom_message",
        "id": "cm1",
        "timestamp": "2026-10-21T22:27:17.000Z",
        "customType": "skill-prompt",
        "content": "[IMPORTANT: User invoked the brainstorming skill]",
    },
    {
        "type": "model_change",
        "id": "mc1",
        "timestamp": "2026-10-21T22:27:18.000Z",
        "model": "openrouter/minimax/minimax-m3.1-flash:free",
    },
]


# ── Parser ───────────────────────────────────────────────────────────────────


class ParseOmpSession(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def test_keeps_only_user_and_assistant_text(self):
        path = session_file(self.root, CONVERSATION)
        _, messages = recall.parse_omp_session(path)
        self.assertEqual(messages, [
            ("user", "add omp support to recall"),
            ("assistant", "omp sessions are a sixth source."),
        ])

    def test_skips_thinking_blocks_and_tool_calls(self):
        path = session_file(self.root, CONVERSATION)
        _, messages = recall.parse_omp_session(path)
        self.assertNotIn("The parser skips thinking blocks.",
                         [text for _, text in messages])

    def test_skips_tool_results(self):
        path = session_file(self.root, CONVERSATION)
        _, messages = recall.parse_omp_session(path)
        self.assertNotIn("file1\nfile2", [text for _, text in messages])

    def test_skips_harness_injected_records(self):
        path = session_file(self.root, CONVERSATION)
        _, messages = recall.parse_omp_session(path)
        self.assertNotIn("[IMPORTANT: User invoked the brainstorming skill]",
                         [text for _, text in messages])

    def test_session_id_comes_from_the_header(self):
        path = session_file(self.root, CONVERSATION, session_id="01a16092-6881-7454-ab80-0d56728b7694")
        metadata, _ = recall.parse_omp_session(path)
        self.assertEqual(metadata["session_id"], "01a16092-6881-7454-ab80-0d56728b7694")
        self.assertEqual(metadata["source"], "omp")
        self.assertEqual(metadata["file_path"], path)

    def test_session_id_falls_back_to_the_file_name(self):
        path = session_file(self.root, CONVERSATION)
        Path(path).write_text(
            json.dumps({"type": "message", "message": {"role": "user", "content": "hi"}}) + "\n",
            encoding="utf-8")
        metadata, _ = recall.parse_omp_session(path)
        self.assertEqual(metadata["session_id"], SESSION_NAME)

    def test_project_comes_from_the_header_cwd(self):
        path = session_file(self.root, CONVERSATION, cwd="/home/u/my project")
        metadata, _ = recall.parse_omp_session(path)
        self.assertEqual(metadata["project"], "/home/u/my project")

    def test_slug_is_the_session_title(self):
        path = session_file(self.root, CONVERSATION, title="🧩 OMPSUP: Add omp support")
        metadata, _ = recall.parse_omp_session(path)
        self.assertEqual(metadata["slug"], "🧩 OMPSUP: Add omp support")

    def test_the_last_title_wins(self):
        path = session_file(self.root, CONVERSATION + [{
            "type": "title_change",
            "id": "tc1",
            "timestamp": "2026-10-21T23:00:00.000Z",
            "title": "A later title",
        }])
        metadata, _ = recall.parse_omp_session(path)
        self.assertEqual(metadata["slug"], "A later title")

    def test_slug_falls_back_to_date_and_short_id_without_a_title(self):
        path = session_file(self.root, CONVERSATION, title=None)
        metadata, _ = recall.parse_omp_session(path)
        self.assertEqual(metadata["slug"], "2026-10-21-01a16092")

    def test_timestamp_comes_from_the_header(self):
        path = session_file(self.root, CONVERSATION)
        metadata, _ = recall.parse_omp_session(path)
        self.assertEqual(metadata["timestamp"],
                         recall.parse_iso_timestamp("2026-10-21T22:26:10.177Z"))

    def test_timestamp_falls_back_to_the_earliest_turn(self):
        path = session_file(self.root, CONVERSATION)
        Path(path).write_text(
            "".join(json.dumps(e) + "\n" for e in [
                turn("user", "hi", ts="2026-10-21T23:00:00.000Z"),
                turn("assistant", "hello", ident="m2", ts="2026-10-21T23:00:01.000Z"),
            ]),
            encoding="utf-8")
        metadata, _ = recall.parse_omp_session(path)
        self.assertEqual(metadata["timestamp"],
                         recall.parse_iso_timestamp("2026-10-21T23:00:00.000Z"))

    def test_a_corrupt_line_costs_only_that_line(self):
        path = session_file(self.root, CONVERSATION)
        with open(path, "a", encoding="utf-8") as f:
            f.write("{not json\n")
            f.write(json.dumps(turn("user", "after the corruption", ident="m9")) + "\n")
        _, messages = recall.parse_omp_session(path)
        self.assertIn("after the corruption", [text for _, text in messages])

    def test_an_empty_transcript_parses(self):
        path = session_file(self.root, [])
        metadata, messages = recall.parse_omp_session(path)
        self.assertEqual(messages, [])
        self.assertEqual(metadata["timestamp"],
                         recall.parse_iso_timestamp("2026-10-21T22:26:10.177Z"))

    def test_a_missing_file_is_reported_not_raised(self):
        self.assertIsNone(recall.parse_omp_session(
            str(Path(self.root) / "nowhere" / "session.jsonl")))


# ── Format detection ─────────────────────────────────────────────────────────


class DetectOmpFormat(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def test_detects_omp_from_its_title_slot(self):
        path = session_file(self.root, CONVERSATION)
        self.assertEqual(read_session.detect_format(path), "omp")

    def test_detects_omp_from_its_header(self):
        path = write_jsonl(Path(self.root) / "s.jsonl", [header(), turn("user", "hi")])
        self.assertEqual(read_session.detect_format(path), "omp")

    def test_reads_the_turns_of_an_omp_session(self):
        path = session_file(self.root, CONVERSATION)
        self.assertEqual(list(read_session.iter_messages(path)), [
            ("user", "add omp support to recall"),
            ("assistant", "omp sessions are a sixth source."),
        ])

    def test_a_pi_session_is_still_detected_as_pi(self):
        path = write_jsonl(Path(self.root) / "p.jsonl", [
            {"type": "session", "version": 3, "id": "019dfd8d", "cwd": "/w"},
            turn("user", "hi"),
        ])
        self.assertEqual(read_session.detect_format(path), "pi")

    def test_a_claude_session_is_still_detected_as_claude(self):
        path = write_jsonl(Path(self.root) / "c.jsonl", [
            {"type": "user", "parentUuid": None, "cwd": "/w",
             "message": {"content": "hi"}},
        ])
        self.assertEqual(read_session.detect_format(path), "claude")


# ── Sessions directory ───────────────────────────────────────────────────────


class OmpSessionsDir(unittest.TestCase):
    def setUp(self):
        for name in ("OMP_SESSION_DIR", "PI_CODING_AGENT_DIR", "XDG_DATA_HOME"):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_the_session_dir_env_var_wins(self):
        os.environ["OMP_SESSION_DIR"] = str(self.root / "sessions")
        self.assertEqual(recall.omp_sessions_dir(), self.root / "sessions")

    def test_the_agent_dir_env_var_moves_the_sessions(self):
        os.environ["PI_CODING_AGENT_DIR"] = str(self.root / "agent")
        self.assertEqual(recall.omp_sessions_dir(), self.root / "agent" / "sessions")

    def test_the_default_is_the_omp_config_root(self):
        self.assertEqual(recall.omp_sessions_dir(),
                         Path.home() / ".omp" / "agent" / "sessions")

    @unittest.skipIf(sys.platform == "win32", "XDG only applies on Linux and macOS")
    def test_xdg_data_home_moves_the_sessions(self):
        xdg = self.root / "xdg"
        (xdg / "omp").mkdir(parents=True)
        os.environ["XDG_DATA_HOME"] = str(xdg)
        self.assertEqual(recall.omp_sessions_dir(), xdg / "omp" / "sessions")


# ── Indexing ─────────────────────────────────────────────────────────────────


class IndexOmpSessions(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()

        for name in ("CLAUDE_PROJECTS_DIR", "CODEX_SESSIONS_DIR",
                     "PI_SESSIONS_DIR", "GROK_SESSIONS_DIR",
                     "CURSOR_PROJECTS_DIR"):
            self.addCleanup(setattr, recall, name, getattr(recall, name))
            setattr(recall, name, self.root / "none")

        self.addCleanup(setattr, recall, "OMP_SESSIONS_DIR",
                        recall.OMP_SESSIONS_DIR)
        recall.OMP_SESSIONS_DIR = self.sessions

        self.addCleanup(os.environ.pop, "OPENCODE_DB", None)
        os.environ["OPENCODE_DB"] = str(self.root / "none" / "opencode.db")

    def write(self, relative, entries=None, **kwargs):
        """Write a session file at <sessions root>/<relative>."""
        path = self.sessions / relative
        records = [{"type": "title", "v": 1, "title": kwargs.get("title", "A title")}]
        records.append(header(cwd=kwargs.get("cwd", "/home/u/project"),
                               session_id=kwargs.get("session_id", SESSION_ID),
                               title=kwargs.get("title", "A title")))
        records.extend(entries if entries is not None else CONVERSATION)
        return write_jsonl(path, records)

    def indexed(self):
        conn = sqlite3.connect(str(self.root / "index.db"))
        recall.create_schema(conn)
        recall.migrate_schema(conn)
        recall.index_sessions(conn)
        conn.commit()
        return conn

    def test_indexes_a_session_with_its_metadata(self):
        self.write("-src-project/" + SESSION_NAME + ".jsonl")
        conn = self.indexed()
        try:
            row = conn.execute(
                "SELECT source, project, slug FROM sessions WHERE session_id = ?",
                (SESSION_ID,)).fetchone()
            self.assertEqual(row, ("omp", "/home/u/project", "A title"))
        finally:
            conn.close()

    def test_indexes_a_subagent_transcript(self):
        self.write("-src-project/" + SESSION_NAME + ".jsonl")
        self.write("-src-project/" + SESSION_NAME + "/ToolAsk.jsonl",
                   session_id="agent-1", title="Subagent work")
        conn = self.indexed()
        try:
            sources = conn.execute(
                "SELECT source FROM sessions WHERE session_id = 'agent-1'").fetchone()
            self.assertEqual(sources, ("omp",))
        finally:
            conn.close()

    def test_the_advisor_transcript_is_not_a_session(self):
        self.write("-src-project/" + SESSION_NAME + ".jsonl")
        self.write("-src-project/" + SESSION_NAME + "/__advisor.jsonl",
                   session_id="advisor-1", title="Advisor notes")
        conn = self.indexed()
        try:
            self.assertIsNone(conn.execute(
                "SELECT source FROM sessions WHERE session_id = 'advisor-1'").fetchone())
        finally:
            conn.close()

    def test_an_omp_session_is_found_by_the_source_filter(self):
        self.write("-src-project/" + SESSION_NAME + ".jsonl")
        conn = self.indexed()
        try:
            hits = recall.search(conn, "recall", source="omp", limit=10)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0][1], "omp")
            self.assertEqual(recall.search(conn, "recall", source="claude", limit=10), [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
