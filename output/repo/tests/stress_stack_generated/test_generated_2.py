import sys
import pytest
from glom.cli import console_main


def test_console_main_success(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["glom", "a", '{"a": 42}'])
    with pytest.raises(SystemExit) as exc_info:
        console_main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "42" in captured.out


def test_console_main_glom_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["glom", "missing_key", '{"a": 42}'])
    with pytest.raises(SystemExit) as exc_info:
        console_main()
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "PathAccessError" in captured.out
