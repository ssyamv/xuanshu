import os


def pytest_sessionstart(session) -> None:
    os.environ.setdefault("XUANSHU_ENV", "test")
