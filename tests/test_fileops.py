from __future__ import annotations

import pytest

from bsmt_mcp.errors import BsmtError
from bsmt_mcp.fileops import replace_in_file, set_action_output


def test_replace_literal(tmp_path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("hello world hello", encoding="utf-8")
    result = replace_in_file(str(path), "hello", "hi")
    assert result["occurrences"] == 2
    assert path.read_text(encoding="utf-8") == "hi world hi"


def test_replace_regex_dry_run(tmp_path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("version=1.2.3\n", encoding="utf-8")
    result = replace_in_file(str(path), r"version=(\d+\.\d+\.\d+)", r"version=\g<1>-beta", use_regex=True, dry_run=True)
    assert result["occurrences"] == 1
    assert path.read_text(encoding="utf-8") == "version=1.2.3\n"


def test_replace_binary_refused(tmp_path) -> None:
    path = tmp_path / "file.bin"
    path.write_bytes(b"a\x00b")
    with pytest.raises(BsmtError):
        replace_in_file(str(path), "a", "b")


def test_set_action_output_appends(tmp_path) -> None:
    path = tmp_path / "github_output"
    set_action_output("filename", "MyMod-1.0.0", str(path))
    set_action_output("assemblyname", "MyMod", str(path))
    assert path.read_text(encoding="utf-8") == "filename=MyMod-1.0.0\nassemblyname=MyMod\n"


def test_set_action_output_requires_path(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    with pytest.raises(BsmtError):
        set_action_output("x", "y")
