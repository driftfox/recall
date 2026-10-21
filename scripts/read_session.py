#!/usr/bin/env python3
"""Pretty-print a Claude Code, Codex, pi, Grok, or opencode session transcript."""

import json
import sys
from pathlib import Path

TEXT_BLOCK_TYPES = {"text", "input_text", "output_text"}

SKIP_MARKERS = (
    "<user_instructions>", "<environment_context>",
    "<permissions instructions>", "# AGENTS.md instructions",
)

# Grok-only: these appear inside genuine Claude user turns (system-reminder
# blocks are appended to real prompts), so they must not be in the shared list.
GROK_SKIP_MARKERS = ("<user_info>", "<system-reminder>", "<git_status>")


def extract_text(content):
    """Extract plain text from message content (string or array format)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type", "") in TEXT_BLOCK_TYPES
        ]
        return "\n".join(filter(None, parts))
    return ""


def iter_messages(path):
    """Yield (role, text) pairs from a session file, auto-detecting format."""
    fmt = detect_format(path)

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Skip Codex state snapshots (legacy)
            if entry.get("record_type") == "state":
                continue

            if fmt == "pi":
                # Pi: {type, id, parentId, timestamp, message: {role, content, ...}}
                # Header is {type: "session", id, cwd, version, ...} — skip.
                etype = entry.get("type", "")
                if etype != "message":
                    continue

                msg = entry.get("message", {})
                if not isinstance(msg, dict):
                    continue

                role = msg.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                content = msg.get("content", "")

            elif fmt == "grok":
                # Grok: top-level type and a content string. Entries carrying
                # synthetic_reason are harness context, not real turns.
                if entry.get("synthetic_reason"):
                    continue
                etype = entry.get("type", "")
                if etype in ("user", "human"):
                    role = "user"
                elif etype == "assistant":
                    role = "assistant"
                else:
                    continue
                content = entry.get("content", "")

            elif fmt == "claude":
                # Resolve role from type or role fields
                role = entry.get("role", "")
                if role not in ("user", "assistant"):
                    etype = entry.get("type", "")
                    if etype in ("user", "human"):
                        role = "user"
                    elif etype == "assistant":
                        role = "assistant"
                    else:
                        continue

                # Claude wraps in entry.message.content
                content = entry.get("message", {})
                if isinstance(content, dict):
                    content = content.get("content", "")
                elif not isinstance(content, str):
                    content = entry.get("content", "")

            else:
                # Codex — handle both legacy and current (wrapped payload) formats
                etype = entry.get("type", "")

                if etype in ("session_meta", "event_msg", "turn_context"):
                    continue

                if etype == "response_item":
                    payload = entry.get("payload", {})
                    role = payload.get("role", "")
                    content = payload.get("content", "")
                else:
                    role = entry.get("role", "")
                    content = entry.get("content", "")

                if role not in ("user", "assistant"):
                    continue

            text = extract_text(content)
            markers = SKIP_MARKERS + (GROK_SKIP_MARKERS if fmt == "grok" else ())
            if not text or any(marker in text for marker in markers):
                continue

            yield role, text


def detect_format(path):
    """Detect whether a session file is Claude Code, Codex, pi, or Grok format.

    Grok is settled by path, since every Grok transcript is named
    chat_history.jsonl inside a per-session directory and its entries are too
    plain to tell apart from the others by content alone.

    Otherwise detection runs on the first non-empty parseable line. Order
    matters: pi headers carry both `type: "session"` and `cwd`, which is the
    most distinctive signature; Claude files have `parentUuid` or a top-level
    `message`; Codex files have `record_type`, `instructions`, or
    `type: "session_meta"`.
    """
    path_obj = Path(path)
    if path_obj.name == "chat_history.jsonl" or "/.grok/sessions/" in str(path_obj):
        return "grok"

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = entry.get("type", "")

            # Pi v2/v3 header: {type: "session", id, cwd, version, ...}.
            # Disambiguates from any "session"-typed entries elsewhere by
            # requiring cwd or version on the same line (only present on the
            # header).
            if etype == "session" and ("cwd" in entry or "version" in entry):
                return "pi"

            if entry.get("record_type") == "state":
                return "codex"
            if "parentUuid" in entry or "message" in entry:
                return "claude"
            if "id" in entry and "instructions" in entry:
                return "codex"
            # Current Codex format uses type: "session_meta"
            if entry.get("type") == "session_meta":
                return "codex"
    return "claude"


def looks_like_opencode(ref):
    """True when `ref` names an opencode session.

    Accepts a bare session id (`ses_...`) or the `<db-path>#<session-id>` value
    recall prints as its File: line.
    """
    import recall
    if not ref:
        return False
    if "#" in ref:
        db_ref, _, session_id = ref.rpartition("#")
        return bool(db_ref) and bool(recall.OPENCODE_SESSION_ID_RE.match(session_id))
    return bool(recall.OPENCODE_SESSION_ID_RE.match(ref))


def iter_opencode_messages(ref):
    """Yield (role, text) pairs for an opencode session reference.

    A `<db-path>#<session-id>` reference reads that database; a bare `ses_...`
    id reads opencode's default database. Reuses recall's reader so the
    transcript matches what recall indexes.
    """
    import recall
    if "#" in ref:
        db_ref, _, session_id = ref.rpartition("#")
        db_path = Path(db_ref)
    else:
        db_path = recall.opencode_db_path()
        session_id = ref
    conn = recall.connect_opencode_db(db_path)
    try:
        yield from recall.opencode_session_messages(conn, session_id)
    finally:
        conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Pretty-print a Claude Code, Codex, pi, Grok, or opencode session transcript")
    parser.add_argument("path", help="Path to a session .jsonl file, or an opencode session id (ses_...) or <db-path>#<session-id>")
    parser.add_argument("--pretty", action="store_true", help="Human-readable output instead of JSON")
    args = parser.parse_args()

    turns = iter_opencode_messages(args.path) if looks_like_opencode(args.path) else iter_messages(args.path)

    if args.pretty:
        for role, text in turns:
            print(f"--- {role} ---")
            print(text[:500])
            print()
    else:
        msgs = [{"role": role, "text": text} for role, text in turns]
        print(json.dumps(msgs, indent=2))


if __name__ == "__main__":
    main()
