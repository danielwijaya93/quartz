"""Unit tests for the responsive top banner formatter."""

from __future__ import annotations

from pathlib import Path

from prompt_toolkit.formatted_text import FormattedText

from quartz.tui.banner import BannerState, _fmt_k, format_banner_lines, format_banner_plain
from quartz.core.loop import TurnUsage


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
    assert "ctx 128.0k (9.7%)" in line
    assert "CH 50.0%" in line
    assert "bg " not in line
    assert len(line) <= 140


def test_narrow_two_line_wrap():
    state = _make_state(bg_count=0)
    lines = format_banner_plain(state, width=70)

    assert len(lines) == 2
    assert lines[0] == "quartz v0.1.0 · gpt-5.4 (high) · ~/workspace/quartz"
    assert lines[1] == "ctx 128.0k (9.7%) · CH 50.0%"
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
    assert wide[0].endswith("· bg x3")

    narrow = format_banner_plain(state, width=70)
    assert len(narrow) == 2
    assert "bg x3" not in narrow[0]
    assert narrow[1].endswith("· bg x3")


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
    assert "ctx 0 (0.0%)" in lines[0]
    assert "CH 0.0%" in lines[0]


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


def test_fmt_k_m_format():
    assert _fmt_k(0) == "0"
    assert _fmt_k(500) == "500"
    assert _fmt_k(999) == "999"
    assert _fmt_k(1000) == "1.0k"
    assert _fmt_k(12_400) == "12.4k"
    assert _fmt_k(128_000) == "128.0k"
    assert _fmt_k(999_999) == "1000.0k"
    assert _fmt_k(1_000_000) == "1.0M"
    assert _fmt_k(1_048_576) == "1.0M"
    assert _fmt_k(1_500_000) == "1.5M"
    assert _fmt_k(2_000_000) == "2.0M"


def test_banner_m_format_context_window():
    # 1M window -> "ctx 1.0M (...)" (spec example `ctx 1M` is the same value
    # rendered with one decimal; the formatter keeps ".0" for symmetry).
    state = _make_state(prompt_tokens=100_000, cached_tokens=50_000, max_context_tokens=1_000_000)
    line = format_banner_plain(state, width=140)[0]
    assert "ctx 1.0M (10.0%)" in line

    state_half = _make_state(prompt_tokens=750_000, cached_tokens=0, max_context_tokens=1_500_000)
    line_half = format_banner_plain(state_half, width=140)[0]
    assert "ctx 1.5M (50.0%)" in line_half


def test_bg_count_only_even_when_bg_jobs_present():
    state = BannerState(
        model="gpt-5.4",
        effort="high",
        cwd=Path.home() / "workspace" / "quartz",
        usage=TurnUsage(prompt_tokens=12_400, cached_tokens=6_200),
        max_context_tokens=128_000,
        bg_count=4,
        bg_jobs=("Run pytest 2m", "git log 5s", "Run sleep 1m"),
        version="0.1.0",
    )
    lines = format_banner_plain(state, width=140)
    assert len(lines) == 1
    assert lines[0].endswith("· bg x4")
    assert "Run pytest" not in lines[0]
    assert "git log" not in lines[0]

    narrow = format_banner_plain(state, width=70)
    assert len(narrow) == 2
    assert narrow[1].endswith("· bg x4")
    assert "Run pytest" not in " ".join(narrow)


def test_turn_footer_compact_format():
    from quartz.tui.render import _format_turn_footer

    usage = TurnUsage(prompt_tokens=12_400, cached_tokens=6_200)
    assert _format_turn_footer(usage, 128_000) == "ctx 128.0k (9.7%) · CH 50.0%"
    assert _format_turn_footer(usage, 1_000_000) == "ctx 1.0M (1.2%) · CH 50.0%"
    zero = TurnUsage(prompt_tokens=0, cached_tokens=0)
    assert _format_turn_footer(zero, 0) == "ctx 0 (0.0%) · CH 0.0%"


def test_banner_overflow_renders_warning():
    from quartz.tui.render import _format_turn_footer

    # 71300 / 64000 = 111.4% — raw pct kept, warning appended.
    usage = TurnUsage(prompt_tokens=71_300, cached_tokens=50_000)
    assert "⚠ overflow" in _format_turn_footer(usage, 64_000)
    state = _make_state(prompt_tokens=71_300, cached_tokens=50_000, max_context_tokens=64_000)
    line = format_banner_plain(state, width=140)[0]
    assert "⚠ overflow" in line
    assert "(111.4%)" in line

    # Exactly 100% is not an overflow.
    assert "overflow" not in _format_turn_footer(
        TurnUsage(prompt_tokens=64_000, cached_tokens=0), 64_000
    )
    # Zero window never overflows.
    assert "overflow" not in _format_turn_footer(TurnUsage(prompt_tokens=999), 0)


def test_banner_ch_one_decimal_shows_99_6_not_100():
    """Real session case: 64000/64230 = 99.64% must read 99.6%, not 100%."""
    from quartz.tui.render import _format_turn_footer

    usage = TurnUsage(prompt_tokens=64_230, cached_tokens=64_000)
    footer = _format_turn_footer(usage, 128_000)
    assert "CH 99.6%" in footer
    assert "CH 100" not in footer
    state = _make_state(prompt_tokens=64_230, cached_tokens=64_000, max_context_tokens=128_000)
    assert "CH 99.6%" in format_banner_plain(state, width=140)[0]
