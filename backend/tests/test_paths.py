from app import paths


def test_not_frozen_by_default() -> None:
    assert paths.is_frozen() is False


def test_app_root_points_to_backend() -> None:
    assert (paths.app_root() / "single_port_app.py").exists()


def test_resource_dir_dev_equals_app_root() -> None:
    assert paths.resource_dir() == paths.app_root()


def test_user_data_dir_is_absolute_and_exists() -> None:
    d = paths.user_data_dir()
    assert d.is_absolute()
    assert d.exists()
