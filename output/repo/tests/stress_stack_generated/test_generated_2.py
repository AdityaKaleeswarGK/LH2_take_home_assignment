import sys

import pytest

from glom.cli import console_main


def test_console_main_evaluates_spec_and_target(capsys, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["glom", "'name'", '{"name": "Ada"}'],
    )

    with pytest.raises(SystemExit) as exc_info:
        console_main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == '"Ada"\n'
