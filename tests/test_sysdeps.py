import types

import pytest

from crtest import sysdeps


def _ok(*args, **kwargs):
    return types.SimpleNamespace(returncode=0, stderr="poppler version 26\n", stdout="")


def test_missing_reports_only_absent_names(monkeypatch):
    monkeypatch.setattr(sysdeps.shutil, "which", lambda name: None if name == "pdftoppm" else "/usr/bin/" + name)
    assert sysdeps.missing(["pdftoppm", "pdftotext"]) == ["pdftoppm"]


def test_missing_is_empty_when_every_tool_is_on_path(monkeypatch):
    monkeypatch.setattr(sysdeps.shutil, "which", lambda name: "/usr/bin/" + name)
    assert sysdeps.missing(["pdftotext"]) == []


def test_require_tools_passes_when_the_binary_runs(monkeypatch):
    monkeypatch.setattr(sysdeps.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(sysdeps.subprocess, "run", _ok)
    sysdeps.require_tools("pdftotext", "pdftoppm")


def test_require_tools_names_a_tool_that_is_not_installed(monkeypatch):
    monkeypatch.setattr(sysdeps.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as exc:
        sysdeps.require_tools("pdftoppm")
    message = str(exc.value)
    assert "pdftoppm: not on PATH" in message
    assert "brew install poppler" in message
    assert "poppler-utils" in message


def test_require_tools_reports_a_binary_that_does_not_start(monkeypatch):
    monkeypatch.setattr(sysdeps.shutil, "which", lambda name: "/opt/homebrew/bin/" + name)

    def crash(*args, **kwargs):
        return types.SimpleNamespace(
            returncode=134,
            stderr="dyld: Library not loaded: /usr/lib/libhvf.dylib\n",
            stdout="",
        )

    monkeypatch.setattr(sysdeps.subprocess, "run", crash)
    with pytest.raises(SystemExit) as exc:
        sysdeps.require_tools("pdftoppm")
    message = str(exc.value)
    assert "failed to run" in message
    assert "libhvf" in message
    assert "brew install poppler" in message


def test_main_defaults_to_poppler_tools(monkeypatch, capsys):
    monkeypatch.setattr(sysdeps.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(sysdeps.subprocess, "run", _ok)
    sysdeps.main([])
    assert capsys.readouterr().out.strip() == "found pdftoppm, pdftotext"
