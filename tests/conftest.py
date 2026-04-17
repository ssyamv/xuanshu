import os


def pytest_sessionstart(session) -> None:
    os.environ["XUANSHU_ENV"] = "test"
