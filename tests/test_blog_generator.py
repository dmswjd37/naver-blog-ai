from pathlib import Path

import pytest

from naver_blog_ai.blog_generator import (
    _format_photo_plan_for_prompt,
    build_photo_analysis_prompt,
    validate_generated_experience_claims,
    validate_photo_marker_plan,
    validate_photo_plan,
    validate_restricted_photo_descriptions,
)


def _group(
    *,
    category: str,
    stage: str,
    order: int,
    token: str,
    caption: str,
) -> dict[str, object]:
    return {
        "category": category,
        "primary_subject": caption,
        "narrative_stage": stage,
        "narrative_order": order,
        "confidence": "high",
        "photo_tokens": [token],
        "caption": caption,
        "visible_content": [caption],
        "allowed_point_indexes": [],
    }


def test_photo_analysis_prompt_separates_display_from_ordered_beverage() -> None:
    prompt = build_photo_analysis_prompt(
        image_paths=[Path("drink.jpg")],
        restaurant_points=["주류 종류가 다양함"],
    )

    assert "첨부 사진을 판독하는 분석기" in prompt
    assert "이 단계에서는 블로그 글을 작성하거나 웹 검색을 하지 않는다" in prompt
    assert "display_or_selection" in prompt
    assert "주류 병이 진열대에 놓인 사진은 beverage가 아니다" in prompt


def test_validate_photo_plan_rejects_category_stage_mismatch() -> None:
    photo_plan = {
        "groups": [
            _group(
                category="menu_or_price",
                stage="cooked_food",
                order=1,
                token="PHOTO_001",
                caption="메뉴판",
            )
        ]
    }

    with pytest.raises(ValueError, match="category와 narrative_stage"):
        validate_photo_plan(
            photo_plan=photo_plan,
            image_paths=[Path("menu.jpg")],
            restaurant_points=[],
        )


def test_photo_plan_is_sorted_by_meal_stage_before_model_order() -> None:
    photo_plan = {
        "groups": [
            _group(
                category="cooking_process",
                stage="cooking",
                order=1,
                token="PHOTO_002",
                caption="굽는 장면",
            ),
            _group(
                category="cooking_process",
                stage="cooking_setup",
                order=2,
                token="PHOTO_001",
                caption="숯불 준비",
            ),
        ]
    }

    formatted = _format_photo_plan_for_prompt(
        photo_plan=photo_plan,
        restaurant_points=[],
    )

    assert formatted.index("PHOTO_001") < formatted.index("PHOTO_002")


def test_validate_photo_marker_plan_rejects_reordered_groups() -> None:
    photo_plan = {
        "groups": [
            _group(
                category="menu_or_price",
                stage="ordering",
                order=1,
                token="PHOTO_001",
                caption="메뉴판",
            ),
            _group(
                category="food",
                stage="cooked_food",
                order=2,
                token="PHOTO_002",
                caption="주문한 음식",
            ),
        ]
    }

    blog_post = "\n".join(
        [
            "[사진:food.jpg | 주문한 음식]",
            "[사진:menu.jpg | 메뉴판]",
        ]
    )

    with pytest.raises(ValueError, match="배치 순서"):
        validate_photo_marker_plan(
            blog_post=blog_post,
            image_paths=[Path("menu.jpg"), Path("food.jpg")],
            photo_plan=photo_plan,
        )


def test_validate_generated_experience_claims_rejects_future_plan() -> None:
    blog_post = """# 제목

테스트 제목

# 본문

다음에는 고량주도 한번 도전해 보고 싶더라구요

[지도]

[태그]
고량주추천
[/태그]
"""

    with pytest.raises(ValueError, match="미래 주문 계획"):
        validate_generated_experience_claims(blog_post)


def test_menu_description_rejects_tasting_experience() -> None:
    photo_plan = {
        "groups": [
            _group(
                category="display_or_selection",
                stage="information",
                order=1,
                token="PHOTO_001",
                caption="주류 진열",
            )
        ]
    }
    blog_post = """[사진:display.jpg | 주류 진열]

진열된 하얼빈 맥주 맛이 좋았어요

[스티커:마무리]
"""

    with pytest.raises(ValueError, match="설명 범위"):
        validate_restricted_photo_descriptions(
            blog_post=blog_post,
            photo_plan=photo_plan,
        )


def test_actual_eating_experience_is_not_reader_recommendation() -> None:
    blog_post = """# 제목

테스트 제목

# 본문

소금에도 찍어 먹어보고 초장에도 찍어 먹었는데 초장이 더 좋았어요

[지도]
"""

    validate_generated_experience_claims(blog_post)


def test_reader_recommendation_is_rejected() -> None:
    blog_post = """# 제목

테스트 제목

# 본문

온면은 같이 주문해 보세요

[지도]
"""

    with pytest.raises(ValueError, match="독자에게 주문"):
        validate_generated_experience_claims(blog_post)


def test_menu_and_display_selection_phrases_are_allowed() -> None:
    photo_plan = {
        "groups": [
            _group(
                category="display_or_selection",
                stage="information",
                order=1,
                token="PHOTO_001",
                caption="주류 진열",
            ),
            _group(
                category="menu_or_price",
                stage="ordering",
                order=2,
                token="PHOTO_002",
                caption="메뉴판",
            ),
        ]
    }
    blog_post = """[사진:display.jpg | 주류 진열]

고량주 종류가 많아서 취향에 맞춰 골라 마시기 좋아 보였어요

[사진:menu.jpg | 메뉴판]

민락2지구양꼬치 먹으러 온 날이라 꼬치 메뉴 중심으로 골랐어요

[스티커:마무리]
"""

    validate_restricted_photo_descriptions(
        blog_post=blog_post,
        photo_plan=photo_plan,
    )
