from pathlib import Path
import tomllib


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _project() -> dict:
    with (BACKEND_DIR / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]


def test_runtime_and_test_http_clients_have_separate_dependencies() -> None:
    project = _project()
    runtime = project["dependencies"]
    dev = project["optional-dependencies"]["dev"]

    assert "fastapi>=0.139.2,<1.0.0" in runtime
    assert any(item.startswith("httpx>=") for item in runtime)
    assert "httpx2>=2.0.0,<3.0.0" in dev


def test_starlette_test_client_uses_httpx2() -> None:
    import starlette.testclient as testclient

    assert testclient.httpx.__name__ == "httpx2"
