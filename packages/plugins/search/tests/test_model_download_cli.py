from __future__ import annotations

import pytest


def test_installed_model_downloader_help_never_fetches(monkeypatch, capsys) -> None:
    from chatcore_search import download_model

    def forbidden():
        raise AssertionError("help must not access the model service")

    monkeypatch.setattr(download_model, "HfApi", forbidden)
    with pytest.raises(SystemExit) as error:
        download_model.main(["--help"])
    assert error.value.code == 0
    assert "usage:" in capsys.readouterr().out
