import subprocess
import sys
from pathlib import Path


def test_package_imports() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    subprocess.run(
        [sys.executable, "-m", "ensurepip", "--upgrade"],
        check=True,
        cwd=repo_root,
    )
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "-e", "."],
        check=True,
        cwd=repo_root,
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlib.metadata import version; import xuanshu; assert xuanshu.__name__ == 'xuanshu'; assert xuanshu.__version__ == version('xuanshu')",
        ],
        check=True,
        cwd=repo_root,
    )

    assert result.returncode == 0
