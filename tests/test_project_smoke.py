from importlib import import_module


def test_package_imports() -> None:
    pkg = import_module("xuanshu")
    assert pkg.__name__ == "xuanshu"
