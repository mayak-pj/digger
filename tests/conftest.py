# -*- coding: utf-8 -*-
import os
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
for p in (APP, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)



def _qt_plugins() -> None:
    """Путь к модулям Qt — явно, до первого окна в тестах: из папки с русскими буквами Qt их сам не находит
    и обрывает процесс без трассировки (самопроверка на Win7, выезд 3). Программа делает то же в `main()`."""
    try:
        from PyQt5.QtCore import QCoreApplication
        from digger.gui.main_window import qt_plugins_dir
    except ImportError:
        return
    plugins = qt_plugins_dir()
    if plugins:
        QCoreApplication.addLibraryPath(plugins)


_qt_plugins()

from digger.core.config import load_config, setup_logging  # noqa: E402
from digger.core.interfaces import Context  # noqa: E402
from digger.core.registry import load_modules  # noqa: E402


@pytest.fixture(scope="session")
def samples_dir():
    d = os.path.join(HERE, "samples")
    if not os.path.isdir(d):
        import make_samples
        make_samples.main()
    return d


@pytest.fixture(scope="session")
def my_test_dir():
    d = os.path.join(APP, "my_test")
    if not os.path.isdir(d):
        pytest.skip("нет папки my_test/")
    return d


@pytest.fixture()
def ctx(tmp_path):
    """Контекст программы: все пути во временной папке, сеть выключена."""
    cfg = load_config(APP)
    cfg["paths"].update({"db": str(tmp_path / "t.sqlite"), "library_dir": str(tmp_path / "lib"),
                         "quarantine_dir": str(tmp_path / "q"), "log_dir": str(tmp_path / "logs")})
    cfg["network"]["offline"] = True
    c = Context(cfg, APP, setup_logging(APP, cfg))
    load_modules(c)
    yield c
    for m in c.modules.values():
        m.close()


def pytest_collection_modifyitems(config, items):
    from digger.recognition.providers.tesseract import find_tesseract
    if shutil.which("tesseract") or find_tesseract(APP):
        return
    skip = pytest.mark.skip(reason="Tesseract не установлен")
    for it in items:
        if "needs_tesseract" in it.keywords:
            it.add_marker(skip)
