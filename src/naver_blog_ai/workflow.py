from __future__ import annotations

import json

from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict

from .blog_generator import generate_blog_post
from .image_loader import load_image_paths
from naver_blog_ai.paths import PROJECT_ROOT


# PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "output" / "blog_post.md"


class RequiredKeyword(TypedDict):
    keyword: str
    title_required: bool
    body_min_count: int


def _load_style_profile() -> dict[str, object]:
    candidates = [
        PROJECT_ROOT / "style_profile.json",
        PROJECT_ROOT / "config" / "style_profile.json",
        PROJECT_ROOT / "data" / "style_profile.json",
    ]

    for path in candidates:
        if path.is_file():
            with path.open("r", encoding="utf-8") as file:
                loaded = json.load(file)

            if not isinstance(loaded, dict):
                raise ValueError(f"style_profile.json의 최상위 값은 객체여야 합니다: {path}")

            return loaded

    searched = "\n".join(f"- {path}" for path in candidates)
    raise FileNotFoundError(
        "style_profile.json을 찾지 못했습니다. 확인한 위치:\n"
        f"{searched}"
    )


def create_blog_post(
    image_dir: Path,
    restaurant_name: str,
    restaurant_location: str,
    restaurant_points: Sequence[str],
    required_keywords: Sequence[RequiredKeyword],
    output_path: Path = DEFAULT_OUTPUT_PATH,
) -> Path:
    """GUI와 CLI에서 공통으로 사용하는 블로그 글 생성 작업."""

    resolved_image_dir = image_dir.resolve()
    resolved_output_path = output_path.resolve()

    if not resolved_image_dir.is_dir():
        raise FileNotFoundError(
            f"이미지 폴더를 찾을 수 없습니다: {resolved_image_dir}"
        )

    if not restaurant_name.strip():
        raise ValueError("가게명을 입력해주세요.")

    cleaned_points = [point.strip() for point in restaurant_points if point.strip()]
    if not cleaned_points:
        raise ValueError("맛집 포인트를 하나 이상 입력해주세요.")

    image_paths = load_image_paths(resolved_image_dir)
    if not image_paths:
        raise ValueError(f"이미지 파일을 찾지 못했습니다: {resolved_image_dir}")

    style_profile = _load_style_profile()

    blog_post = generate_blog_post(
        image_paths=image_paths,
        restaurant_name=restaurant_name.strip(),
        restaurant_location=restaurant_location.strip(),
        restaurant_points=cleaned_points,
        required_keywords=required_keywords,
        style_profile=style_profile,
    )

    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_output_path.write_text(blog_post, encoding="utf-8")

    return resolved_output_path
