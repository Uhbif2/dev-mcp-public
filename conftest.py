"""Публичная версия: тесты, сверяющие боевую конфигурацию владельца, пропускаются.

В приватном репозитории они проверяют живые policy.toml, fleet-projects.toml, расширение
вкладок и проекты флота; здесь этих файлов нет намеренно (tools/public_export.py).
"""
import pytest

def pytest_collection_modifyitems(config, items):
    mark = pytest.mark.skip(reason="needs the owner's production configuration (not published)")
    for item in items:
        if item.get_closest_marker("production") is not None:
            item.add_marker(mark)
