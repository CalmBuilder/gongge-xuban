from pathlib import Path

from app import paths


def test_new_data_directory_is_used_for_fresh_install(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GONGGE_XUBAN_DATA_DIR", raising=False)
    monkeypatch.setattr(paths, "_platform_data_parent", lambda: tmp_path)

    assert paths.user_data_dir() == tmp_path / "Gongge-Xuban"


def test_unrecognized_data_directory_is_not_reused(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GONGGE_XUBAN_DATA_DIR", raising=False)
    foreign_name = "".join(("Staff", "Deck"))
    (tmp_path / foreign_name).mkdir()
    monkeypatch.setattr(paths, "_platform_data_parent", lambda: tmp_path)

    assert paths.user_data_dir() == tmp_path / "Gongge-Xuban"


def test_new_data_directory_wins_when_both_exist(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GONGGE_XUBAN_DATA_DIR", raising=False)
    current = tmp_path / "Gongge-Xuban"
    current.mkdir()
    (tmp_path / "".join(("Staff", "Deck"))).mkdir()
    monkeypatch.setattr(paths, "_platform_data_parent", lambda: tmp_path)

    assert paths.user_data_dir() == current
