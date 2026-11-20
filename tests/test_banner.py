"""Unit tests for the responsive top banner formatter."""

from __future__ import annotations

from pathlib import Path

from prompt_toolkit.formatted_text import FormattedText

from agent.banner import BannerState, format_banner_lines, format_banner_plain
from agent.loop import TurnUsage


def _make_state(
    *,
    model: str = "gpt-5.4",
    effort: str | None = "high",
    cwd: str | Path | None = None,
    prompt_tokens: int = 12_400,
    cached_tokens: int = 6_200,
    max_context_tokens: int = 128_000,
    bg_count: int = 0,
    version: str = "0.1.0",
) -> BannerState:
    usage = TurnUsage(prompt_tokens=prompt_tokens, cached_tokens=cached_tokens)
    return BannerState(
        model=model,
        effort=effort,
        cwd=cwd if cwd is not None else Path.home() / "workspace" / "quartz",
        usage=usage,
        max_context_tokens=max_context_tokens,
        bg_count=bg_count,
        version=version,
    )


def test_wide_one_line_layout():
    state = _make_state(bg_count=0)
    lines = format_banner_plain(state, width=140)

    assert len(lines) == 1
    line = lines[0]
    assert "quartz v0.1.0" in line
    assert "gpt-5.4 (high)" in line
    assert "~/workspace/quartz" in line
    assert "ctx 12.4k/128.0k (9.7%)" in line
    assert "cache 50%" in line
    assert "bg " not in line
    assert len(line) <= 140


def test_narrow_two_line_wrap():
    state = _make_state(bg_count=0)
    lines = format_banner_plain(state, width=70)

    assert len(lines) == 2
    assert lines[0] == "quartz v0.1.0 · gpt-5.4 (high) · ~/workspace/quartz"
    assert lines[1] == "ctx 12.4k/128.0k (9.7%) · cache 50%"
    assert all(len(line) <= 70 for line in lines)


def test_effort_absent_omits_parentheses():
    for effort in (None, ""):
        state = _make_state(effort=effort)
        lines = format_banner_plain(state, width=140)
        assert len(lines) == 1
        assert "gpt-5.4 ·" in lines[0]
        assert "()" not in lines[0]


def test_effort_not_duplicated_when_model_name_ends_with_effort():
    state = _make_state(model="gemini-3.8-flash-high", effort="high")
    lines = format_banner_plain(state, width=140)
    assert len(lines) == 1
    assert "gemini-3.8-flash-high ·" in lines[0]
    assert "(high)" not in lines[0]

    state_diff = _make_state(model="gemini-3.8-flash-high", effort="medium")
    lines_diff = format_banner_plain(state_diff, width=140)
    assert "gemini-3.8-flash-high (medium)" in lines_diff[0]


def test_bg_count_shown_when_positive_on_both_layouts():
    state = _make_state(bg_count=3)

    wide = format_banner_plain(state, width=140)
    assert len(wide) == 1
    assert wide[0].endswith("· bg 3")

    narrow = format_banner_plain(state, width=70)
    assert len(narrow) == 2
    assert "bg 3" not in narrow[0]
    assert narrow[1].endswith("· bg 3")


def test_home_path_collapse():
    home = Path.home()

    at_home = format_banner_plain(_make_state(cwd=home), width=140)[0]
    assert " · ~ · " in at_home

    under_home = format_banner_plain(_make_state(cwd=home / "projects" / "app"), width=140)[0]
    assert " · ~/projects/app · " in under_home

    outside_home = format_banner_plain(_make_state(cwd="/tmp/quartz-demo"), width=140)[0]
    assert " · /tmp/quartz-demo · " in outside_home


def test_zero_tokens_and_zero_max_context():
    state = _make_state(prompt_tokens=0, cached_tokens=0, max_context_tokens=0)
    lines = format_banner_plain(state, width=140)

    assert len(lines) == 1
    assert "ctx 0/0 (0.0%)" in lines[0]
    assert "cache 0%" in lines[0]


def test_extreme_narrow_widths_truncate_gracefully():
    long_cwd = Path.home() / "workspace" / "very" / "deeply" / "nested" / "repository"
    state = _make_state(cwd=long_cwd, bg_count=2)

    for width in (40, 25, 10, 1, 0):
        expected_max = max(1, width)
        lines = format_banner_plain(state, width=width)
        assert 1 <= len(lines) <= 2
        for line in lines:
            assert len(line) <= expected_max

    lines_40 = format_banner_plain(state, width=40)
    assert len(lines_40) == 2
    assert "…" in lines_40[0]


def test_formatted_lines_match_plain_and_prompt_toolkit_compatible():
    state = _make_state(bg_count=1)
    for width in (140, 70, 30):
        formatted_lines = format_banner_lines(state, width=width)
        plain_lines = format_banner_plain(state, width=width)

        assert len(formatted_lines) == len(plain_lines)
        for fragments, plain in zip(formatted_lines, plain_lines, strict=True):
            ft = FormattedText(fragments)
            assert "".join(text for _, text in ft) == plain
