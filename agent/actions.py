"""Static action classifier for ipython tool calls."""

from __future__ import annotations

import ast
import re
import shlex

_CATEGORY_ORDER = ("list_dir", "find", "read", "edit", "mcp", "run")

_SINGLE_LABELS: dict[str, str] = {
    "list_dir": "List directory and files",
    "find": "Find files",
    "read": "Read files",
    "edit": "Edit files",
    "mcp": "Calling MCP",
    "run": "Run program or script",
}

_SHORT_LABELS: dict[str, str] = {
    "list_dir": "List directory",
    "find": "Find files",
    "read": "Read files",
    "edit": "Edit files",
    "mcp": "Calling MCP",
    "run": "Run script",
}

_FILE_VERBS: dict[str, str] = {
    "find": "Find",
    "read": "Read",
    "edit": "Edit",
}

_BASH_CALL_RE = re.compile(r"\bbash(?:_bg)?\s*\(\s*([\"'])(.*?)\1", re.DOTALL)


def _attr_chain(node: ast.AST) -> list[str]:
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    parts.reverse()
    return parts


def _classify_bash_command(cmd_str: str, found: set[str]) -> None:
    """Classify shell commands inside a bash(...) call."""
    cleaned = re.sub(r"\d*>&\d+", "", cmd_str)
    cleaned = re.sub(r">+\s*/dev/null", "", cleaned)
    if re.search(r"(?:^|[^<])>{1,2}\s*\S+", cleaned):
        found.add("edit")

    segments = re.split(r"\s*(?:&&|\|\||;|\||\n)\s*", cmd_str)
    for seg in segments:
        seg = seg.strip()
        if not seg or seg.startswith("#"):
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        idx = 0
        while idx < len(tokens) and (
            ("=" in tokens[idx] and not tokens[idx].startswith("-")) or tokens[idx] in ("sudo", "env")
        ):
            idx += 1
        if idx >= len(tokens):
            continue
        prog = tokens[idx].rsplit("/", 1)[-1]
        args = tokens[idx + 1 :]

        if prog in ("ls", "exa", "eza", "tree", "dir"):
            found.add("list_dir")
        elif prog in ("rg", "grep", "ag", "ack", "find", "fd", "fdfind", "locate"):
            found.add("find")
        elif prog in ("cat", "head", "tail", "less", "more", "bat", "nl", "wc"):
            found.add("read")
        elif prog == "sed":
            if any(a == "-i" or a.startswith("-i") for a in args):
                found.add("edit")
            else:
                found.add("read")
        elif prog == "awk":
            found.add("read")
        elif prog in ("touch", "mkdir", "rm", "rmdir", "mv", "cp", "chmod", "chown", "ln", "tee", "truncate"):
            found.add("edit")
        elif prog in ("echo", "printf"):
            continue
        else:
            found.add("run")


def classify_cell_actions(code: str) -> list[str]:
    """Return ordered action category keys detected in an ipython cell."""
    found: set[str] = set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            chain = _attr_chain(node.func)
            if not chain:
                continue
            root = chain[0]
            name = chain[-1]

            if root == "mcp" or name in ("call_mcp", "call_mcp_tool"):
                found.add("mcp")
                continue

            if name in ("bash", "bash_bg"):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    _classify_bash_command(node.args[0].value, found)
                else:
                    found.add("run")
                continue

            if name in ("listdir", "scandir", "iterdir", "walk") or chain == ["ls"]:
                found.add("list_dir")
            elif name in ("glob", "rglob", "iglob") or chain in (["find"], ["rg"]):
                found.add("find")
            elif name == "open":
                mode = "r"
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                    mode = node.args[1].value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        mode = kw.value.value
                if any(ch in mode for ch in ("w", "a", "x", "+")):
                    found.add("edit")
                else:
                    found.add("read")
            elif name in ("read_text", "read_bytes", "readlines", "readline") or chain == ["read"]:
                found.add("read")
            elif name in (
                "write_text",
                "write_bytes",
                "writelines",
                "mkdir",
                "makedirs",
                "unlink",
                "rmdir",
                "rename",
                "rmtree",
                "copy",
                "copy2",
                "copytree",
                "move",
            ) or chain in (["edit"], ["write"]):
                found.add("edit")
            elif (
                len(chain) >= 2
                and chain[0] == "subprocess"
                and name in ("run", "Popen", "check_output", "check_call", "call")
            ) or chain == ["os", "system"]:
                found.add("run")
    else:
        for match in _BASH_CALL_RE.finditer(code):
            _classify_bash_command(match.group(2), found)
        if "mcp." in code:
            found.add("mcp")

    return [cat for cat in _CATEGORY_ORDER if cat in found]


def format_action_labels(categories: list[str]) -> str:
    """Format one or more action category keys into a human-readable label."""
    if not categories:
        return "Run Python"
    if len(categories) == 1:
        return _SINGLE_LABELS[categories[0]]

    if all(cat in _FILE_VERBS for cat in categories):
        verbs = [_FILE_VERBS[cat] for cat in categories]
        if len(verbs) == 2:
            return f"{verbs[0]} and {verbs[1]} files"
        return f"{', '.join(verbs[:-1])}, and {verbs[-1]} files"

    parts = [_SHORT_LABELS[cat] for cat in categories]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def describe_cell_actions(code: str) -> str:
    """Return a descriptive action label for a single ipython cell."""
    return format_action_labels(classify_cell_actions(code))


def describe_group_actions(codes: list[str]) -> str:
    """Return a combined descriptive action label across multiple ipython cells."""
    combined: set[str] = set()
    for code in codes:
        combined.update(classify_cell_actions(code))
    ordered = [cat for cat in _CATEGORY_ORDER if cat in combined]
    return format_action_labels(ordered)
