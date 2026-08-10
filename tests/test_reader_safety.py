from __future__ import annotations

import ast
import unittest
from pathlib import Path


FORBIDDEN_CALLS = {
    "click",
    "delete_messages",
    "download_media",
    "edit_message",
    "forward_to",
    "forward_messages",
    "get_contacts",
    "get_dialogs",
    "get_messages",
    "get_participants",
    "get_sender",
    "iter_dialogs",
    "iter_messages",
    "iter_participants",
    "log_out",
    "reply",
    "respond",
    "send_read_acknowledge",
    "send_file",
    "send_message",
    "send_reaction",
}

FORBIDDEN_TYPES = {
    "ImportChatInviteRequest",
    "InviteToChannelRequest",
    "JoinChannelRequest",
    "SendMessageRequest",
}


class ReaderSafetyTests(unittest.TestCase):
    def test_reader_has_no_send_join_or_participant_enumeration_path(self) -> None:
        root = Path(__file__).resolve().parents[1] / "reader"
        violations: list[str] = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (
                    node.module or ""
                ).startswith("telethon.tl.functions"):
                    violations.append(
                        f"{path.name}:{node.lineno}:raw Telegram requests import"
                    )
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in FORBIDDEN_CALLS:
                        violations.append(f"{path.name}:{node.lineno}:{node.func.attr}")
                    if node.func.attr == "get_entity" and path.name != "resolver.py":
                        violations.append(f"{path.name}:{node.lineno}:get_entity")
                if isinstance(node, ast.Name) and node.id in FORBIDDEN_TYPES:
                    violations.append(f"{path.name}:{node.lineno}:{node.id}")
                if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_TYPES:
                    violations.append(f"{path.name}:{node.lineno}:{node.attr}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
