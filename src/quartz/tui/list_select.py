"""Pure selection helpers shared by the inline option and searchable modals."""

from __future__ import annotations


def filter_options(options: list[tuple[str, str]], query: str) -> list[tuple[str, str]]:
    """Case-insensitive AND-match of whitespace-separated tokens against value or label."""
    tokens = query.strip().lower().split()
    if not tokens:
        return options
    return [
        (val, lbl)
        for val, lbl in options
        if all(tok in val.lower() or tok in lbl.lower() for tok in tokens)
    ]


def clamp_index(idx: int, total: int) -> int:
    """Clamp ``idx`` into ``[0, total - 1]`` (0 for an empty list)."""
    if total <= 0:
        return 0
    return max(0, min(idx, total - 1))


def move_index(idx: int, delta: int, total: int) -> int:
    """Move the selection by ``delta`` with wrap-around."""
    if total <= 0:
        return 0
    return (idx + delta) % total


def page_index(idx: int, direction: int, total: int, page_size: int) -> int:
    """Move by a page without wrapping (PgUp = -1, PgDn = +1)."""
    return clamp_index(idx + direction * max(1, page_size), total)


def visible_window(total: int, idx: int, max_visible: int) -> tuple[int, int]:
    """Return ``(start, end)`` rows to render, keeping ``idx`` roughly centred."""
    if total <= 0:
        return 0, 0
    max_vis = max(1, min(max_visible, total))
    idx = clamp_index(idx, total)
    start = max(0, min(idx - max_vis // 2, total - max_vis))
    return start, min(total, start + max_vis)
