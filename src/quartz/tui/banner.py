"""Top banner formatting for Quartz's interactive prompt/TUI."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from quartz.core.loop import TurnUsage

type StyleFragment = tuple[str, str]
type FormattedLine = list[StyleFragment]

_SEP = " · "

_STYLE_BRAND = "class:banner.brand bold"
_STYLE_DIM = "class:banner.dim dim"
_STYLE_MODEL = "class:banner.model bold ansicyan"
_STYLE_CWD = "class:banner.cwd"
_STYLE_METRIC = "class:banner.metric"
_STYLE_BG = "class:banner.bg bold ansiyellow"


@dataclasses.dataclass(frozen=True)
class BannerState:
    """Snapshot of session state rendered into the pinned top banner."""

    model: str
    effort: str | None
    cwd: str | Path
    usage: TurnUsage
    max_context_tokens: int
    bg_count: int = 0
    bg_jobs: tuple[str, ...] = ()
    version: str = "0.1.0"


def _fmt_k(n: int) -> str:
    """Format token counts compactly: k for thousands, M for millions.

    Examples: 0 -> "0", 500 -> "500", 12400 -> "12.4k", 128000 -> "128.0k",
    1000000 -> "1.0M", 1500000 -> "1.5M".
    """
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _short_path(path: str | Path) -> str:
    """Render a path with the home directory collapsed to `~`."""
    text = str(path)
    try:
        home = str(Path.home())
    except (RuntimeError, OSError):
        return text
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home) :]
    return text


def _truncate_cwd(cwd: str, max_len: int) -> str:
    """Truncate a collapsed path to fit within ``max_len``, preserving the tail."""
    if len(cwd) <= max_len:
        return cwd
    if max_len <= 0:
        return ""
    if max_len == 1:
        return "…"
    return "…" + cwd[-(max_len - 1) :]


def _plain_len(fragments: FormattedLine) -> int:
    return sum(len(text) for _, text in fragments)


def _truncate_fragments(fragments: FormattedLine, max_width: int) -> FormattedLine:
    """Truncate a list of style fragments so its plain text length <= ``max_width``."""
    if _plain_len(fragments) <= max_width:
        return fragments
    if max_width <= 0:
        return []
    if max_width == 1:
        style = fragments[0][0] if fragments else ""
        return [(style, "…")]

    target = max_width - 1
    result: FormattedLine = []
    remaining = target
    last_style = ""
    for style, text in fragments:
        if not text:
            continue
        if remaining <= 0:
            break
        last_style = style
        if len(text) <= remaining:
            result.append((style, text))
            remaining -= len(text)
        else:
            result.append((style, text[:remaining]))
            remaining = 0
            break
    result.append((last_style, "…"))
    return result


def _join_groups(groups: list[FormattedLine]) -> FormattedLine:
    """Join styled fragment groups with the banner separator."""
    out: FormattedLine = []
    for idx, group in enumerate(groups):
        if idx > 0:
            out.append((_STYLE_DIM, _SEP))
        out.extend(group)
    return out


def _build_identity_groups(state: BannerState, cwd_text: str) -> list[FormattedLine]:
    brand_group: FormattedLine = [
        (_STYLE_BRAND, "quartz"),
        (_STYLE_DIM, f" v{state.version}"),
    ]
    show_effort = bool(state.effort) and not state.model.lower().endswith(f"-{state.effort.lower()}")
    model_label = f"{state.model} ({state.effort})" if show_effort else state.model
    model_group: FormattedLine = [(_STYLE_MODEL, model_label)]
    cwd_group: FormattedLine = [(_STYLE_CWD, cwd_text)]
    return [brand_group, model_group, cwd_group]


def format_usage_metrics(usage: TurnUsage, max_context_tokens: int) -> tuple[str, str]:
    """Return ``(ctx_value, cache_value)`` strings for banner and turn footer.

    Shared by ``_build_metrics_groups`` and ``_format_turn_footer`` so the
    compact banner and footer can never drift: ``ctx`` is the raw window
    percentage plus a visible ``⚠ overflow`` suffix when the last prompt
    exceeds the window; ``CH`` is one decimal so e.g. 64000/64230 (99.6%)
    is not rounded to a misleading "100%".
    """
    pct = usage.context_pct(max_context_tokens) if max_context_tokens > 0 else 0.0
    cache_pct = usage.cache_hit_rate * 100.0
    ctx_value = f"{_fmt_k(max_context_tokens)} ({pct:.1f}%)"
    if usage.is_overflowing(max_context_tokens):
        ctx_value += " ⚠ overflow"
    cache_value = f"{cache_pct:.1f}%"
    return ctx_value, cache_value


def _build_metrics_groups(state: BannerState) -> list[FormattedLine]:
    ctx_value, cache_value = format_usage_metrics(state.usage, state.max_context_tokens)

    groups: list[FormattedLine] = [
        [(_STYLE_DIM, "ctx "), (_STYLE_METRIC, ctx_value)],
        [(_STYLE_DIM, "CH "), (_STYLE_METRIC, cache_value)],
    ]
    if state.bg_count > 0:
        groups.append([(_STYLE_BG, f"bg x{state.bg_count}")])
    return groups


def format_banner_lines(state: BannerState, width: int) -> list[FormattedLine]:
    """Return 1 or 2 prompt_toolkit FormattedText lines for the pinned top banner."""
    clamped_width = max(1, int(width))
    cwd_text = _short_path(state.cwd)

    identity_groups = _build_identity_groups(state, cwd_text)
    metrics_groups = _build_metrics_groups(state)

    single_line = _join_groups(identity_groups + metrics_groups)
    if _plain_len(single_line) <= clamped_width:
        return [single_line]

    # Narrow layout: wrap into 2 lines, truncating cwd first if line 1 overflows.
    prefix_groups = identity_groups[:2]
    prefix_line = _join_groups(prefix_groups)
    avail_for_cwd = clamped_width - _plain_len(prefix_line) - len(_SEP)
    if avail_for_cwd >= 1 and len(cwd_text) > avail_for_cwd:
        cwd_text = _truncate_cwd(cwd_text, avail_for_cwd)
        identity_groups = _build_identity_groups(state, cwd_text)

    line1 = _truncate_fragments(_join_groups(identity_groups), clamped_width)
    line2 = _truncate_fragments(_join_groups(metrics_groups), clamped_width)
    return [line1, line2]


def format_banner_plain(state: BannerState, width: int) -> list[str]:
    """Return plain-text version of format_banner_lines for testing and plain rendering."""
    return ["".join(text for _, text in line) for line in format_banner_lines(state, width)]
