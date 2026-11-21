from __future__ import annotations

from prompt_toolkit.buffer import Buffer

from quartz.tui.list_select import clamp_index, filter_options, move_index, page_index, visible_window
from quartz.tui.modals import InlineModalState, _ModalListControl

OPTS = [("a1", "Alpha one"), ("b2", "Beta two"), ("c3", "Gamma three")]


def test_filter_options() -> None:
    assert filter_options(OPTS, "") == OPTS
    assert filter_options(OPTS, "  ") == OPTS
    assert filter_options(OPTS, "BETA") == [("b2", "Beta two")]
    assert filter_options(OPTS, "a two") == [("b2", "Beta two")]  # AND of tokens
    assert filter_options(OPTS, "c3") == [("c3", "Gamma three")]  # matches value
    assert filter_options(OPTS, "zzz") == []


def test_index_helpers() -> None:
    assert clamp_index(5, 3) == 2 and clamp_index(-1, 3) == 0 and clamp_index(1, 0) == 0
    assert move_index(2, 1, 3) == 0 and move_index(0, -1, 3) == 2 and move_index(0, 1, 0) == 0
    assert page_index(0, 1, 30, 10) == 10
    assert page_index(25, 1, 30, 10) == 29  # no wrap
    assert page_index(3, -1, 30, 10) == 0


def test_visible_window() -> None:
    assert visible_window(0, 0, 5) == (0, 0)
    assert visible_window(3, 1, 15) == (0, 3)
    assert visible_window(100, 0, 10) == (0, 10)
    assert visible_window(100, 50, 10) == (45, 55)
    assert visible_window(100, 99, 10) == (90, 100)


def test_option_list_is_windowed_and_shows_counts() -> None:
    options = [(str(i), f"item {i}") for i in range(50)]
    modal = InlineModalState(
        kind="option", title="T", buffer=Buffer(), options=options, selected_idx=25, max_visible=10
    )
    content = _ModalListControl(lambda: modal).create_content(60, 40)
    text = ["".join(t for _, t in content.get_line(i)) for i in range(content.line_count)]
    assert any("more above" in line for line in text)
    assert any("more below" in line and "50 total" in line for line in text)
    assert any("❯ item 25" in line for line in text)
    assert content.line_count <= 1 + 1 + 10 + 1  # header + above + rows + below


def test_searchable_filter_uses_shared_helper() -> None:
    buf = Buffer()
    modal = InlineModalState(kind="searchable", title="T", buffer=buf, options=OPTS)
    buf.text = "gamma"
    assert modal.filtered_options() == [("c3", "Gamma three")]
