"""Inspector file loading must not depend on the Windows locale."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from jev_ultrafast import demo


@pytest.fixture
def gbk_default(monkeypatch):
    read_text = Path.read_text

    def windows_read_text(path, encoding=None, errors=None):
        return read_text(path, encoding=encoding or "gbk", errors=errors)

    monkeypatch.setattr(Path, "read_text", windows_read_text)


@pytest.mark.parametrize("url,name", [
    ("/", "index.html"),
    ("/app.js", "app.js"),
    ("/style.css", "style.css"),
    ("/fixture.html", "fixture.html"),
])
def test_static_routes_with_gbk_default(gbk_default, url, name):
    handler = demo.Handler.__new__(demo.Handler)
    handler.headers = {"Host": f"127.0.0.1:{demo.PORT}"}
    handler.path = url
    handler.send = Mock()

    handler.do_GET()

    status, content, mime = handler.send.call_args.args
    assert status == 200
    expected = (demo.ROOT / "static" / name).read_bytes().decode("utf-8")
    assert content.splitlines() == expected.replace("__TOKEN__", demo.TOKEN).splitlines()
    assert mime.endswith("charset=utf-8")


def test_environment_with_utf8_bom_and_gbk_default(gbk_default, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JEV_TEST_LABEL", raising=False)
    monkeypatch.setenv("JEV_TEST_EXISTING", "process-value")
    (tmp_path / ".env").write_bytes(
        "JEV_TEST_LABEL=\u4e2d\u6587\nJEV_TEST_EXISTING=file-value\n".encode("utf-8-sig")
    )

    demo.load_environment()

    assert demo.os.environ["JEV_TEST_LABEL"] == "\u4e2d\u6587"
    assert demo.os.environ["JEV_TEST_EXISTING"] == "process-value"
