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

_BASH_CALL_RE = re.compile(r"\bbash\s*\(\s*([\"'])(.*?)\1", re.DOTALL)


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

            if root == "mcp" or name == "call_mcp_tool":
                found.add("mcp")
                continue

            if name == "bash":
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


# ---------------------------------------------------------------------------
# Tool-intent summaries (describe_cell_intent)
# ---------------------------------------------------------------------------
# Short, human-readable "what is this cell doing" labels derived statically
# from helper/sample-AST calls. Unlike describe_cell_actions (category
# buckets), these name the actual target: "Read agent/ui.py (200-400)",
# "Run pytest", 'Web search: "S&P 500 level"'. Defensive: never raises.

_MAX_INTENT_LEN = 60
_SH_FALLBACK_RE = re.compile(r"\b(?:sh|bash)\s*\(\s*([\"'])(.*?)\1", re.DOTALL)


def _shorten_path(path: str) -> str:
    """Collapse the home directory to ``~``; defensive, never raises."""
    try:
        from quartz.tui.banner import _short_path  # local import: no cycle
    except Exception:
        try:
            import os
            from pathlib import Path

            home = str(Path.home())
        except Exception:
            return path
        if path == home:
            return "~"
        if path.startswith(home + os.sep):
            return "~" + path[len(home):]
        return path
    try:
        return _short_path(path)
    except Exception:
        return path


def _shorten_url(url: str) -> str:
    """Return host + path for a URL (dropping scheme/query), else the raw string."""
    text = url.strip()
    if not text:
        return text
    try:
        from urllib.parse import urlparse

        parsed = urlparse(text)
        if parsed.netloc:
            host = parsed.netloc.split("@")[-1]
            path = parsed.path if parsed.path and parsed.path != "/" else ""
            return f"{host}{path}"
    except Exception:
        pass
    return text


def _intent_const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _intent_const_int(node: ast.AST) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    return None


def _kw_str(node: ast.Call, name: str) -> str | None:
    for kw in node.keywords:
        if kw.arg == name:
            return _intent_const(kw.value)
    return None


def _kw_int(node: ast.Call, name: str) -> int | None:
    for kw in node.keywords:
        if kw.arg == name:
            return _intent_const_int(kw.value)
    return None


def _strip_env_prefix(tokens: list[str]) -> list[str]:
    """Drop leading ``VAR=value`` assignments and ``sudo``/``env`` tokens."""
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in ("sudo", "env"):
            idx += 1
            continue
        if "=" in tok and not tok.startswith("-") and not tok.startswith("="):
            head = tok.split("=", 1)[0]
            if head and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", head):
                idx += 1
                continue
        break
    return tokens[idx:]


def _is_prompt_indirection(prompt: str) -> bool:
    """Return True when ``prompt`` is launcher plumbing, not a real prompt.

    ``launch_worker`` builds ``quartz -p "$(cat <prompt_file>)"``: the prompt
    token is a shell indirection (``$(cat ...)``, backticks, ``cat <file>``).
    Such tokens carry zero information about the job, so they must never become
    its label.
    """
    text = prompt.strip()
    if not text:
        return True
    if text.startswith(("$(", "`", "$'", "$(cat", "`cat", "cat ")):
        return True
    return False


def _describe_quartz_command(args: list[str]) -> str:
    """Label a ``quartz`` invocation (e.g. forked workers launched by bash()).

    ``uv run quartz -p "<prompt>" -r <sid> --fork --cwd <worktree>`` ->
    ``Quartz: <prompt snippet> · <worktree basename>``. Launcher plumbing
    (``-p "$(cat <prompt_file>)"``) is dropped instead: ``Quartz worker``
    (plus the worktree basename). Bare ``quartz`` invocations fall back to
    ``Run quartz``.
    """
    prompt: str | None = None
    prompt_present = False
    cwd: str | None = None
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if tok in ("-p", "--prompt") and idx + 1 < len(args):
            prompt = args[idx + 1]
            prompt_present = True
            idx += 2
            continue
        if tok.startswith("--prompt="):
            prompt = tok.split("=", 1)[1]
            prompt_present = True
        elif tok.startswith("-p") and len(tok) > 2:
            prompt = tok[2:]
            prompt_present = True
        elif tok == "--cwd" and idx + 1 < len(args):
            cwd = args[idx + 1]
            idx += 2
            continue
        elif tok.startswith("--cwd="):
            cwd = tok.split("=", 1)[1]
        idx += 1

    base = ""
    if cwd and cwd.strip() and cwd.strip() != ".":
        base = cwd.rstrip("/").rsplit("/", 1)[-1]
    parts: list[str] = []
    if prompt_present:
        if prompt is not None and prompt.strip() and not _is_prompt_indirection(prompt):
            snippet = " ".join(prompt.split())
            if len(snippet) > 30:
                snippet = snippet[:29].rstrip() + "…"
            parts.append(f"Quartz: {snippet}")
        else:
            parts.append("Quartz worker")
    else:
        parts.append("Run quartz")
    if base:
        parts.append(base)
    result = " · ".join(parts)
    if len(result) > _MAX_INTENT_LEN:
        result = result[: _MAX_INTENT_LEN - 1].rstrip() + "…"
    return result


def _describe_shell_command(cmd_str: str) -> str | None:
    """Map one shell command string to a short intent label (or None)."""
    if not cmd_str or not cmd_str.strip():
        return None
    # First segment only: `&&`/`||`/`|`/`;`/newline chains are not the
    # primary action, and the suffix is usually log noise.
    segment = re.split(r"\s*(?:&&|\|\||;|\||\n)\s*", cmd_str.strip(), maxsplit=1)[0]
    segment = segment.strip()
    if not segment or segment.startswith("#"):
        return None
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    tokens = _strip_env_prefix(tokens)
    if not tokens:
        return None
    prog = tokens[0].rsplit("/", 1)[-1]
    args = tokens[1:]

    if prog == "uv" and args and args[0] == "run":
        rest = _strip_env_prefix(args[1:])
        if not rest:
            return "Run uv"
        sub = rest[0].rsplit("/", 1)[-1]
        if sub == "pytest" or (sub == "python" and "-m" in rest and "pytest" in rest):
            return "Run pytest"
        if sub == "quartz":
            return _describe_quartz_command(rest[1:])
        return f"Run {sub}"
    if prog == "quartz":
        return _describe_quartz_command(args)
    if prog == "pytest":
        return "Run pytest"
    if prog == "python" and "-m" in args and "pytest" in args:
        return "Run pytest"
    if prog == "git":
        sub = next((a for a in args if not a.startswith("-")), None)
        if sub:
            return f"git {sub}"
        return "git"
    if prog == "gh":
        if len(args) >= 2 and args[0] == "pr" and not args[1].startswith("-"):
            return f"gh pr {args[1]}"
        sub = next((a for a in args if not a.startswith("-")), None)
        return f"gh {sub}" if sub else "gh"
    if prog in ("cat", "head", "tail"):
        files = [a for a in args if not a.startswith("-")]
        if files:
            return f"Read {_shorten_path(files[-1])}"
        return f"Run {prog}"
    if prog in ("grep", "rg"):
        positional = [a for a in args if not a.startswith("-")]
        # Drop options that consume the next token (-e PAT, -m N, ...).
        skip_next = False
        cleaned: list[str] = []
        for a in args:
            if skip_next:
                skip_next = False
                continue
            if a in ("-e", "-m", "-A", "-B", "-C", "-g", "--glob", "--max-count"):
                skip_next = True
                continue
            if a.startswith("-"):
                continue
            cleaned.append(a)
        if cleaned:
            pattern = cleaned[0]
            if len(cleaned) > 1:
                return f'Search "{pattern}" in {_shorten_path(cleaned[-1])}'
            return f'Search "{pattern}"'
        return f"Run {prog}"
    if prog in ("ls", "exa", "eza", "tree", "dir"):
        paths = [a for a in args if not a.startswith("-")]
        if paths:
            return f"List {_shorten_path(paths[-1])}"
        return "List"
    if prog in ("echo", "printf"):
        return None
    return f"Run {prog}"


def _describe_shell_wrapper(node: ast.Call) -> str | None:
    """Describe a sh(...)/bash(...) call from its first positional arg, if a str constant."""
    if not node.args:
        return None
    cmd = _intent_const(node.args[0])
    if cmd is None:
        return None
    return _describe_shell_command(cmd)


def _describe_call_intent(node: ast.Call) -> str | None:
    chain = _attr_chain(node.func)
    if not chain:
        return None
    root = chain[0]
    name = chain[-1]

    # mcp.<server>.<tool>(...)
    if root == "mcp" and len(chain) >= 3:
        server, tool = chain[1], chain[2]
        lowered = tool.lower()
        if "search" in lowered:
            query = _kw_str(node, "query")
            if query is None and node.args:
                query = _intent_const(node.args[0])
            if query:
                query = " ".join(query.split())
                if len(query) > 40:
                    query = query[:40].rstrip() + "…"
                return f"Web search: {query}"
            return f"Web search via {server}"
        if "fetch" in lowered:
            url = _kw_str(node, "url") or _kw_str(node, "uri")
            if url is None and node.args:
                url = _intent_const(node.args[0])
            if url:
                return f"Fetch {_shorten_url(url)}"
            return f"Fetch via {server}"
        return f"MCP {server}.{tool}"
    if name == "call_mcp_tool":
        return "MCP call"

    # Shell wrappers (helpers + kernel builtin).
    if name in ("sh", "bash"):
        return _describe_shell_wrapper(node)

    # Kernel helpers.
    if name == "read":
        path = _intent_const(node.args[0]) if node.args else None
        if path is None:
            path = _kw_str(node, "path")
        if path is None:
            return None
        short = _shorten_path(path)
        start = _intent_const_int(node.args[1]) if len(node.args) > 1 else None
        end = _intent_const_int(node.args[2]) if len(node.args) > 2 else None
        if start is None:
            start = _kw_int(node, "start")
        if start is None:
            kw_start = _kw_str(node, "start")
            start = int(kw_start) if kw_start and kw_start.lstrip("-").isdigit() else None
        if end is None:
            end = _kw_int(node, "end")
        if end is None:
            kw_end = _kw_str(node, "end")
            end = int(kw_end) if kw_end and kw_end.lstrip("-").isdigit() else None
        if start is not None and start != 1:
            if end is not None:
                return f"Read {short} ({start}-{end})"
            return f"Read {short} (from {start})"
        return f"Read {short}"
    if name == "edit":
        path = _intent_const(node.args[0]) if node.args else _kw_str(node, "path")
        return f"Edit {_shorten_path(path)}" if path else None
    if name == "write":
        path = _intent_const(node.args[0]) if node.args else _kw_str(node, "path")
        return f"Write {_shorten_path(path)}" if path else None
    if name == "view_image":
        path = _intent_const(node.args[0]) if node.args else _kw_str(node, "path")
        return f"View {_shorten_path(path)}" if path else None
    if name == "ls":
        path = _intent_const(node.args[0]) if node.args else _kw_str(node, "path")
        if not path or path == ".":
            return "List"
        return f"List {_shorten_path(path)}"
    if name == "find":
        pattern = _intent_const(node.args[0]) if len(node.args) > 0 else _kw_str(node, "pattern")
        path = _intent_const(node.args[1]) if len(node.args) > 1 else _kw_str(node, "path")
        if not pattern:
            return None
        if path and path != ".":
            return f"Find {pattern} in {_shorten_path(path)}"
        return f"Find {pattern}"
    if name == "rg":
        pattern = _intent_const(node.args[0]) if len(node.args) > 0 else _kw_str(node, "pattern")
        path = _intent_const(node.args[1]) if len(node.args) > 1 else _kw_str(node, "path")
        if not pattern:
            return None
        if path and path != ".":
            return f'Search "{pattern}" in {_shorten_path(path)}'
        return f'Search "{pattern}"'
    if name == "now":
        return "Check date/time"
    return None


def _intents_from_source(code: str) -> list[str]:
    """Collect ordered, deduped intent labels from one cell's source."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # Parser broke — at least rescue sh()/bash() string args via regex.
        found: list[str] = []
        for match in _SH_FALLBACK_RE.finditer(code):
            try:
                label = _describe_shell_command(match.group(2))
            except Exception:
                label = None
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
            continue
        if label is None:
            continue
        if label == "Check date/time":
            # Only emitted when nothing else describes the cell.
            saw_now = True
            continue
        if label not in labels:
            labels.append(label)
    if not labels and saw_now:
        return ["Check date/time"]
    return labels


def describe_cell_intent(code: str) -> str:
    """Return a short "what is this cell doing" label for one ipython cell.

    Examples: "Read agent/ui.py (200-400)", "Run pytest",
    "Web search: S&P 500 level", "Run Python" (no identifiable operation).
    Defensive: never raises, always returns a non-empty string.
    """
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
    result = " ".join(str(result).split())
    if len(result) > _MAX_INTENT_LEN:
        result = result[: _MAX_INTENT_LEN - 1].rstrip() + "…"
    return result or "Run Python"


def describe_job_label(command: str) -> str:
    """Return a short label for a background shell command (pure, never raises).

    Used for banner/progress visibility of `bash()` jobs. Falls back to the
    first program token so identity is always visible, e.g. "Run pytest",
    "git log", 'Search "foo" in agent/', "Run uv".
    """
    try:
        label = _describe_shell_command(command or "")
    except Exception:
        label = None
    if label:
        return label
    try:
        text = (command or "").strip()
        if not text:
            return "Background job"
        segment = re.split(r"\s*(?:&&|\|\||;|\||\n)\s*", text, maxsplit=1)[0].strip()
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        tokens = _strip_env_prefix(tokens)
        if not tokens:
            return "Background job"
        prog = tokens[0].rsplit("/", 1)[-1]
    except Exception:
        prog = "Background job"
    result = f"Run {prog}" if prog else "Background job"
    if len(result) > _MAX_INTENT_LEN:
        result = result[: _MAX_INTENT_LEN - 1].rstrip() + "…"
    return result or "Background job"
