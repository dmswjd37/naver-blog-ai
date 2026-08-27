import sys

from pathlib import Path

def get_project_root() -> Path:
    """
    개발 PC에서는 pyproject.toml이 있는 프로젝트 루트를 사용하고,
    배포 환경에서는 EXE가 있는 폴더를 사용한다.
    """

    if getattr(sys, "frozen", False):
        search_start = Path(sys.executable).resolve().parent
    else:
        search_start = Path(__file__).resolve().parent

    for candidate in (
        search_start,
        *search_start.parents,
    ):
        if (candidate / "pyproject.toml").is_file():
            return candidate

    # 배포받은 PC에는 pyproject.toml이 없으므로 EXE 폴더 사용
    return search_start

PROJECT_ROOT = get_project_root()