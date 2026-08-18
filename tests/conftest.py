import pytest

import akita_zmodem_meshcore as azm


@pytest.fixture(autouse=True)
def isolated_default_config(tmp_path, monkeypatch):
    """Keep tests from reading or rewriting a developer's local config file."""
    monkeypatch.setattr(
        azm, "CONFIG_FILE", str(tmp_path / "akita_zmodem_meshcore_config.json"))
