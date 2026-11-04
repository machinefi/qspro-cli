"""Surgical JSON/JSONC object edits, preserving comments and unrelated bytes."""

from __future__ import annotations

import json
import re

from .machines import MachineError

TOKEN = re.compile(
    r'\s+|//[^\n]*|/\*[\s\S]*?\*/|"(?:[^"\\]|\\.)*"|[{}\[\]:,]|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null'
)


class Node:
    def __init__(self, start, end, value, children=None):
        self.start, self.end, self.value = start, end, value
        self.children = children or {}


def parse(text):
    tokens, offset = [], 0
    for match in TOKEN.finditer(text):
        if text[offset : match.start()].strip():
            raise MachineError(
                "invalid_config", "Cannot edit malformed JSON/JSONC configuration.", 2
            )
        offset = match.end()
        token = match.group()
        if not token.isspace() and not token.startswith(("//", "/*")):
            tokens.append((token, match.start(), match.end()))
    if text[offset:].strip():
        raise MachineError("invalid_config", "Cannot edit malformed JSON/JSONC configuration.", 2)
    cursor = 0

    def value():
        nonlocal cursor
        token, start, end = tokens[cursor]
        cursor += 1
        if token in ("{", "["):
            obj = {} if token == "{" else []
            children = {}
            closing = "}" if token == "{" else "]"
            while tokens[cursor][0] != closing:
                if token == "{":
                    key_start = tokens[cursor][1]
                    key = json.loads(tokens[cursor][0])
                    if not isinstance(key, str) or key in obj:
                        raise ValueError("Duplicate/nonstring key")
                    cursor += 1
                    if tokens[cursor][0] != ":":
                        raise ValueError("Missing colon")
                    cursor += 1
                    child = value()
                    child.key_start = key_start
                    obj[key], children[key] = child.value, child
                else:
                    obj.append(value().value)
                if tokens[cursor][0] != ",":
                    break
                cursor += 1
            if tokens[cursor][0] != closing:
                raise ValueError("Missing closing token")
            end = tokens[cursor][2]
            cursor += 1
            return Node(start, end, obj, children)
        return Node(start, end, json.loads(token))

    try:
        root = value()
        if cursor != len(tokens) or not isinstance(root.value, dict):
            raise ValueError()
        return root
    except (ValueError, IndexError, TypeError):
        raise MachineError(
            "invalid_config", "Cannot edit malformed or ambiguous JSON/JSONC configuration.", 2
        )


def get_entry(text, section, key):
    root = parse(text)
    parent = root.children.get(section)
    if parent is None:
        return None
    if not isinstance(parent.value, dict):
        raise MachineError("invalid_config", "MCP configuration section must be an object.", 2)
    node = parent.children.get(key)
    return node.value if node else None


def set_entry(text, section, key, entry):
    root = parse(text)
    parent = root.children.get(section)
    if parent is None:
        if entry is None:
            return text
        return insert(text, root, section, {key: entry})
    if not isinstance(parent.value, dict):
        raise MachineError("invalid_config", "MCP configuration section must be an object.", 2)
    node = parent.children.get(key)
    if node is not None:
        if entry is not None:
            return text[: node.start] + json.dumps(entry, ensure_ascii=True) + text[node.end :]
        # Token positions keep quoted text inside comments out of the edited span.
        start = node.key_start
        tail = node.end
        # Skip whitespace/comments while locating commas; preserve comments elsewhere.
        meaningful = [
            (m.group(), m.start(), m.end())
            for m in TOKEN.finditer(text[node.end : parent.end - 1])
            if not m.group().isspace() and not m.group().startswith(("//", "/*"))
        ]
        if meaningful and meaningful[0][0] == ",":
            tail = node.end + meaningful[0][2]
        else:
            prior = [
                (m.group(), m.start(), m.end())
                for m in TOKEN.finditer(text[parent.start : start])
                if not m.group().isspace() and not m.group().startswith(("//", "/*"))
            ]
            if prior and prior[-1][0] == ",":
                start = parent.start + prior[-1][1]
        return text[:start] + text[tail:]
    return text if entry is None else insert(text, parent, key, entry)


def insert(text, parent, key, entry):
    at = parent.end - 1
    before = text[:at]
    tokens = [
        m.group()
        for m in TOKEN.finditer(text[parent.start : at])
        if not m.group().isspace() and not m.group().startswith(("//", "/*"))
    ]
    comma = "," if parent.value and tokens[-1] != "," else ""
    # A newline before a comma also terminates a preceding line comment.
    return (
        before
        + "\n"
        + comma
        + json.dumps(key)
        + ": "
        + json.dumps(entry, ensure_ascii=True)
        + "\n"
        + text[at:]
    )
