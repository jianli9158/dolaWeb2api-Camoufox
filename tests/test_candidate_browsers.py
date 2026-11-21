"""Tests for native Camoufox binary discovery (replaces Playwright Chromium channel tests)."""

import os
from pathlib import Path
from doubao2api.camoufox_fingerprint import find_camoufox_executable


def test_find_camoufox_executable_env_override(tmp_path, monkeypatch):
    fake_bin = tmp_path / "camoufox-bin"
    fake_bin.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    os.chmod(fake_bin, 0o755)
    monkeypatch.setenv("CAMOUFOX_PATH", str(fake_bin))

    found = find_camoufox_executable()
    assert found == str(fake_bin)


def test_find_camoufox_executable_dola_env_override(tmp_path, monkeypatch):
    fake_bin = tmp_path / "dola-camoufox"
    fake_bin.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    os.chmod(fake_bin, 0o755)
    monkeypatch.delenv("CAMOUFOX_PATH", raising=False)
    monkeypatch.setenv("DOLA_BROWSER_BIN", str(fake_bin))

    found = find_camoufox_executable()
    assert found == str(fake_bin)
