"""Tests for static action classification and intent labeling."""

from __future__ import annotations

import warnings

from quartz.kernel.actions import classify_cell_actions, describe_cell_intent


def test_invalid_escape_sequence_emits_no_syntax_warning() -> None:
    code = r'rg("\(foo\)")'.replace('\\\\', '\\')
    # Verify our test string actually has single backslashes like rg("\(foo\)")
    code = 'rg("' + chr(92) + '(foo' + chr(92) + ')")'
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", SyntaxWarning)
        actions = classify_cell_actions(code)
        intent = describe_cell_intent(code)

    assert [w for w in caught if issubclass(w.category, SyntaxWarning)] == []
    assert actions == ["find"]
    assert "Search" in intent
