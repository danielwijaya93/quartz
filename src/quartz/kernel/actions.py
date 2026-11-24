"""Static action classifier and intent summarizer for ipython tool calls."""

from __future__ import annotations

import ast
import contextlib
import logging
import os
import re
import shlex
import warnings
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_CATEGORY_ORDER = ("list_dir", "find", "read", "edit", "mcp", "run")
_SINGLE_LABELS = {
    "list_dir": "List directory and files",
    "find": "Find files",
    "read": "Read files",
    "edit": "Edit files",
    "mcp": "Calling MCP",
    "run": "Run program or script",
}
_SHORT_LABELS = {
    "list_dir": "List directory",
    "find": "Find files",
    "read": "Read files",
    "edit": "Edit files",
    "mcp": "Calling MCP",
    "run": "Run script",
}
_FILE_VERBS = {"find": "Find", "read": "Read", "edit": "Edit"}
_LIST_PROGS = frozenset({"ls", "exa", "eza", "tree", "dir"})
_FIND_PROGS = frozenset({"rg", "grep", "ag", "ack", "find", "fd", "fdfind", "locate"})
_READ_PROGS = frozenset({"cat", "head", "tail", "less", "more", "bat", "nl", "wc", "awk"})
_EDIT_PROGS = frozenset({"touch", "mkdir", "rm", "rmdir", "mv", "cp", "chmod", "chown", "ln", "tee", "truncate"})
_EDIT_CALLS = frozenset({
    "write_text", "write_bytes", "writelines", "mkdir", "makedirs", "unlink",
    "rmdir", "rename", "rmtree", "copy", "copy2", "copytree", "move",
})
_SEARCH_VALUE_FLAGS = frozenset({"-e", "-m", "-A", "-B", "-C", "-g", "--glob", "--max-count"})
_BASH_CALL_RE = re.compile(r"\bbash\s*\(\s*([\"'])(.*?)\1", re.DOTALL)
_SH_FALLBACK_RE = re.compile(r"\b(?:sh|bash)\s*\(\s*([\"'])(.*?)\1", re.DOTALL)
_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
_ENV_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_INTENT_LEN = 60


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


def _parse_ast(code: str) -> ast.Module:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=SyntaxWarning)
        return ast.parse(code, "<ipython>", "exec")


def _strip_env_prefix(tokens: list[str], *, strict_ident: bool = True) -> list[str]:
    """Drop leading ``VAR=value`` assignments and ``sudo``/``env`` tokens."""
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in ("sudo", "env"):
            idx += 1
            continue
        if "=" in tok and not tok.startswith(("-", "=")):
            head = tok.split("=", 1)[0]
            if not strict_ident or (head and _ENV_VAR_RE.fullmatch(head)):
                idx += 1
                continue
        break
    return tokens[idx:]


def _tokenize_segment(seg: str, *, strict_ident: bool = True) -> tuple[str, list[str]] | None:
    seg = seg.strip()
    if not seg or seg.startswith("#"):
        return None
    try:
        tokens = shlex.split(seg)
    except ValueError:
        tokens = seg.split()
    tokens = _strip_env_prefix(tokens, strict_ident=strict_ident)
    if not tokens:
        return None
    return tokens[0].rsplit("/", 1)[-1], tokens[1:]


def _classify_bash_command(cmd_str: str, found: set[str]) -> None:
    """Classify shell commands inside a bash(...) call."""
    cleaned = re.sub(r">+/dev/null|>+\s*/dev/null|\d*>&\d+", "", cmd_str)
    if re.search(r"(?:^|[^<])>{1,2}\s*\S+", cleaned):
        found.add("edit")

    for seg in _SEGMENT_SPLIT_RE.split(cmd_str):
        parsed = _tokenize_segment(seg, strict_ident=False)
        if parsed is None:
            continue
        prog, args = parsed
        if prog in _LIST_PROGS:
            found.add("list_dir")
        elif prog in _FIND_PROGS:
            found.add("find")
        elif prog in _READ_PROGS:
            found.add("read")
        elif prog == "sed":
            found.add("edit" if any(a.startswith("-i") for a in args) else "read")
        elif prog in _EDIT_PROGS:
            found.add("edit")
        elif prog not in ("echo", "printf"):
            found.add("run")


def _intent_const(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _intent_const_int(node: ast.AST) -> int | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool) else None


def _kw_str(node: ast.Call, name: str) -> str | None:
    return next((_intent_const(kw.value) for kw in node.keywords if kw.arg == name), None)


def _kw_int(node: ast.Call, name: str) -> int | None:
    return next((_intent_const_int(kw.value) for kw in node.keywords if kw.arg == name), None)


def _arg_str(node: ast.Call, pos: int, *kw_names: str) -> str | None:
    if len(node.args) > pos:
        val = _intent_const(node.args[pos])
        if val is not None:
            return val
    for name in kw_names:
        val = _kw_str(node, name)
        if val is not None:
            return val
    return None


def _arg_int(node: ast.Call, pos: int, kw_name: str) -> int | None:
    if len(node.args) > pos:
        val = _intent_const_int(node.args[pos])
        if val is not None:
            return val
    val = _kw_int(node, kw_name)
    if val is not None:
        return val
    raw = _kw_str(node, kw_name)
    return int(raw) if raw and raw.lstrip("-").isdigit() else None


def classify_cell_actions(code: str) -> list[str]:
    """Return ordered action category keys detected in an ipython cell."""
    found: set[str] = set()
    try:
        tree: ast.Module | None = _parse_ast(code)
    except SyntaxError:
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            chain = _attr_chain(node.func)
            if not chain:
                continue
            root, name = chain[0], chain[-1]
            if root == "mcp" or name == "call_mcp_tool":
                found.add("mcp")
            elif name == "bash":
                cmd = _intent_const(node.args[0]) if node.args else None
                if cmd is not None:
                    _classify_bash_command(cmd, found)
                else:
                    found.add("run")
            elif name in ("listdir", "scandir", "iterdir", "walk") or chain == ["ls"]:
                found.add("list_dir")
            elif name in ("glob", "rglob", "iglob") or chain in (["find"], ["rg"]):
                found.add("find")
            elif name == "open":
                mode = _arg_str(node, 1, "mode") or "r"
                found.add("edit" if any(ch in mode for ch in ("w", "a", "x", "+")) else "read")
            elif name in ("read_text", "read_bytes", "readlines", "readline") or chain == ["read"]:
                found.add("read")
            elif name in _EDIT_CALLS or chain in (["edit"], ["write"]):
                found.add("edit")
            elif (
                len(chain) >= 2 and root == "subprocess" and name in ("run", "Popen", "check_output", "check_call", "call")
            ) or chain == ["os", "system"]:
                found.add("run")
    else:
        for match in _BASH_CALL_RE.finditer(code):
            _classify_bash_command(match.group(2), found)
        if "mcp." in code:
            found.add("mcp")

    return [cat for cat in _CATEGORY_ORDER if cat in found]


def _join_labels(items: list[str]) -> str:
    return f"{items[0]} and {items[1]}" if len(items) == 2 else f"{', '.join(items[:-1])}, and {items[-1]}"


def format_action_labels(categories: list[str]) -> str:
    """Format one or more action category keys into a human-readable label."""
    if not categories:
        return "Run Python"
    if len(categories) == 1:
        return _SINGLE_LABELS[categories[0]]
    if all(cat in _FILE_VERBS for cat in categories):
        return f"{_join_labels([_FILE_VERBS[cat] for cat in categories])} files"
    return _join_labels([_SHORT_LABELS[cat] for cat in categories])


def describe_cell_actions(code: str) -> str:
    """Return a descriptive action label for a single ipython cell."""
    return format_action_labels(classify_cell_actions(code))


def describe_group_actions(codes: list[str]) -> str:
    """Return a combined descriptive action label across multiple ipython cells."""
    combined: set[str] = set()
    for code in codes:
        combined.update(classify_cell_actions(code))
    return format_action_labels([cat for cat in _CATEGORY_ORDER if cat in combined])


def _truncate_intent(text: str, limit: int = _MAX_INTENT_LEN) -> str:
    return text[: limit - 1].rstrip() + "…" if len(text) > limit else text


def _shorten_path(path: str) -> str:
    """Collapse the home directory to ``~``; defensive, never raises."""
    try:
        home = str(Path.home())
    except Exception:
        return path
    if path == home:
        return "~"
    return "~" + path[len(home) :] if path.startswith(home + os.sep) else path


def _shorten_url(url: str) -> str:
    """Return host + path for a URL (dropping scheme/query), else the raw string."""
    text = url.strip()
    if text:
        with contextlib.suppress(ValueError):
            parsed = urlparse(text)
            if parsed.netloc:
                host = parsed.netloc.split("@")[-1]
                path = parsed.path if parsed.path and parsed.path != "/" else ""
                return f"{host}{path}"
    return text


def _is_prompt_indirection(prompt: str) -> bool:
    """Return True when ``prompt`` is launcher plumbing, not a real prompt."""
    text = prompt.strip()
    return not text or text.startswith(("$(", "`", "$'", "$(cat", "`cat", "cat "))


def _describe_quartz_command(args: list[str]) -> str:
    """Label a ``quartz`` invocation (e.g. forked workers launched by bash())."""
    prompt: str | None = None
    prompt_present = False
    cwd: str | None = None
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if tok in ("-p", "--prompt") and idx + 1 < len(args):
            prompt, prompt_present, idx = args[idx + 1], True, idx + 2
            continue
        if tok.startswith("--prompt="):
            prompt, prompt_present = tok.split("=", 1)[1], True
        elif tok.startswith("-p") and len(tok) > 2:
            prompt, prompt_present = tok[2:], True
        elif tok == "--cwd" and idx + 1 < len(args):
            cwd, idx = args[idx + 1], idx + 2
            continue
        elif tok.startswith("--cwd="):
            cwd = tok.split("=", 1)[1]
        idx += 1

    base = cwd.rstrip("/").rsplit("/", 1)[-1] if cwd and cwd.strip() not in ("", ".") else ""
    if prompt_present:
        if prompt and prompt.strip() and not _is_prompt_indirection(prompt):
            snippet = _truncate_intent(" ".join(prompt.split()), 30)
            parts = [f"Quartz: {snippet}"]
        else:
            parts = ["Quartz worker"]
    else:
        parts = ["Run quartz"]
    if base:
        parts.append(base)
    return _truncate_intent(" · ".join(parts))


def _describe_shell_command(cmd_str: str) -> str | None:
    """Map one shell command string to a short intent label (or None)."""
    if not cmd_str or not cmd_str.strip():
        return None
    head = _SEGMENT_SPLIT_RE.split(cmd_str.strip(), maxsplit=1)[0]
    parsed = _tokenize_segment(head, strict_ident=True)
    if parsed is None:
        return None
    prog, args = parsed

    if prog == "uv" and args and args[0] == "run":
        rest = _strip_env_prefix(args[1:])
        if not rest:
            return "Run uv"
        sub = rest[0].rsplit("/", 1)[-1]
        if sub == "pytest" or (sub == "python" and "-m" in rest and "pytest" in rest):
            return "Run pytest"
        return _describe_quartz_command(rest[1:]) if sub == "quartz" else f"Run {sub}"
    if prog == "quartz":
        return _describe_quartz_command(args)
    if prog.startswith("python") and len(args) >= 2 and args[:2] == ["-m", "quartz"]:
        return _describe_quartz_command(args[2:])
    if prog == "pytest" or (prog == "python" and "-m" in args and "pytest" in args):
        return "Run pytest"
    if prog == "git":
        sub = next((a for a in args if not a.startswith("-")), None)
        return f"git {sub}" if sub else "git"
    if prog == "gh":
        if len(args) >= 2 and args[0] == "pr" and not args[1].startswith("-"):
            return f"gh pr {args[1]}"
        sub = next((a for a in args if not a.startswith("-")), None)
        return f"gh {sub}" if sub else "gh"
    if prog in ("cat", "head", "tail"):
        files = [a for a in args if not a.startswith("-")]
        return f"Read {_shorten_path(files[-1])}" if files else f"Run {prog}"
    if prog in ("grep", "rg"):
        skip_next = False
        cleaned: list[str] = []
        for a in args:
            if skip_next:
                skip_next = False
            elif a in _SEARCH_VALUE_FLAGS:
                skip_next = True
            elif not a.startswith("-"):
                cleaned.append(a)
        if not cleaned:
            return f"Run {prog}"
        return f'Search "{cleaned[0]}" in {_shorten_path(cleaned[-1])}' if len(cleaned) > 1 else f'Search "{cleaned[0]}"'
    if prog in _LIST_PROGS:
        paths = [a for a in args if not a.startswith("-")]
        return f"List {_shorten_path(paths[-1])}" if paths else "List"
    return None if prog in ("echo", "printf") else f"Run {prog}"


_PATH_HELPER_VERBS = {"edit": "Edit", "write": "Write", "view_image": "View", "attach_file": "Attach"}


def _describe_call_intent(node: ast.Call) -> str | None:
    chain = _attr_chain(node.func)
    if not chain:
        return None
    root, name = chain[0], chain[-1]

    if root == "mcp" and len(chain) >= 3:
        server, tool = chain[1], chain[2]
        lowered = tool.lower()
        if "search" in lowered:
            query = _kw_str(node, "query") or (_intent_const(node.args[0]) if node.args else None)
            if query:
                q = " ".join(query.split())
                return f"Web search: {q[:40].rstrip() + '…' if len(q) > 40 else q}"
            return f"Web search via {server}"
        if "fetch" in lowered:
            url = _kw_str(node, "url") or _kw_str(node, "uri") or (_intent_const(node.args[0]) if node.args else None)
            return f"Fetch {_shorten_url(url)}" if url else f"Fetch via {server}"
        return f"MCP {server}.{tool}"
    if name == "call_mcp_tool":
        return "MCP call"
    if name in ("sh", "bash"):
        cmd = _intent_const(node.args[0]) if node.args else None
        return _describe_shell_command(cmd) if cmd is not None else None
    if name == "read":
        path = _arg_str(node, 0, "path")
        if path is None:
            return None
        short = _shorten_path(path)
        start, end = _arg_int(node, 1, "start"), _arg_int(node, 2, "end")
        if start is not None and start != 1:
            return f"Read {short} ({start}-{end})" if end is not None else f"Read {short} (from {start})"
        return f"Read {short}"
    if name in _PATH_HELPER_VERBS:
        path = _arg_str(node, 0, "path")
        return f"{_PATH_HELPER_VERBS[name]} {_shorten_path(path)}" if path else None
    if name == "ls":
        path = _arg_str(node, 0, "path")
        return f"List {_shorten_path(path)}" if path and path != "." else "List"
    if name in ("find", "rg"):
        pattern, path = _arg_str(node, 0, "pattern"), _arg_str(node, 1, "path")
        if not pattern:
            return None
        prefix = f"Find {pattern}" if name == "find" else f'Search "{pattern}"'
        return f"{prefix} in {_shorten_path(path)}" if path and path != "." else prefix
    return "Check date/time" if name == "now" else None


def _intents_from_source(code: str) -> list[str]:
    """Collect ordered, deduped intent labels from one cell's source."""
    try:
        tree = _parse_ast(code)
    except SyntaxError:
        found: list[str] = []
        for match in _SH_FALLBACK_RE.finditer(code):
            with contextlib.suppress(Exception):
                label = _describe_shell_command(match.group(2))
                if label and label not in found:
                    found.append(label)
        return found
    except Exception:
        return []

    labels: list[str] = []
    saw_now = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        try:
            label = _describe_call_intent(node)
        except Exception:
            logger.debug("Call intent inspection failed", exc_info=True)
            continue
        if label == "Check date/time":
            saw_now = True
        elif label and label not in labels:
            labels.append(label)
    return labels if labels else (["Check date/time"] if saw_now else [])


def describe_cell_intent(code: str) -> str:
    """Return a short "what is this cell doing" label for one ipython cell (never raises)."""
    try:
        labels = _intents_from_source(code or "")
    except Exception:
        labels = []
    if not labels:
        return "Run Python"
    if len(labels) == 1:
        result = labels[0]
    elif len(labels) == 2:
        result = f"{labels[0]} · {labels[1]}"
    else:
        result = f"{labels[0]} · {labels[1]} · +{len(labels) - 2} more"
    return _truncate_intent(" ".join(str(result).split())) or "Run Python"


def describe_job_label(command: str) -> str:
    """Return a short label for a background shell command (pure, never raises)."""
    with contextlib.suppress(Exception):
        label = _describe_shell_command(command or "")
        if label:
            return label
    with contextlib.suppress(Exception):
        head = _SEGMENT_SPLIT_RE.split((command or "").strip(), maxsplit=1)[0]
        parsed = _tokenize_segment(head, strict_ident=True)
        if parsed is not None:
            return _truncate_intent(f"Run {parsed[0]}")
    return "Background job"
