from __future__ import annotations

import json
import re
import shutil
import subprocess

from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from naver_blog_ai.paths import PROJECT_ROOT


# PROJECT_ROOT = Path(__file__).resolve().parents[2]
CODEX_TIMEOUT_SECONDS = 900
MAX_GENERATION_ATTEMPTS = 3
MAX_PHOTO_ANALYSIS_ATTEMPTS = 2

PHOTO_ANALYSIS_SCHEMA_PATH = (
    PROJECT_ROOT
    / "config"
    / "photo_analysis.schema.json"
)

PHOTO_ANALYSIS_CATEGORIES = {
    "place_exterior",
    "place_interior",
    "facility_or_service",
    "notice_or_information",
    "menu_or_price",
    "table_setting",
    "side_dish",
    "food",
    "beverage",
    "cooking_process",
    "eating_process",
    "sauce_or_condiment",
    "dessert",
    "receipt_or_payment",
    "other",
}

PHOTO_MARKER_RE = re.compile(
    r"\[(?:사진|사진묶음|대표사진)\s*:"
    r"\s*(?P<filenames>[^|\]]+)"
)

PHOTO_MARKER_FULL_RE = re.compile(
    r"\[(?P<kind>사진|사진묶음|대표사진)\s*:"
    r"\s*(?P<filenames>[^|\]]+)"
    r"(?P<description>\s*\|[^\]]*)?\]"
)

DUPLICATE_PHOTO_SENTINEL = "[중복사진자동제거]"

HEADING_RE = re.compile(
    r"^[ \t]*(?:\*{1,2})?[ \t]*#{1,6}[ \t]*"
    r"(?P<name>제목|본문)"
    r"[ \t]*(?:\*{1,2})?[ \t]*$",
    re.MULTILINE,
)

TAG_START_RE = re.compile(
    r"^[ \t]*(?:"
    r"(?:\*{1,2})?[ \t]*#{1,6}[ \t]*태그[ \t]*(?:\*{1,2})?"
    r"|\[태그\]"
    r")[ \t]*$",
    re.MULTILINE,
)


def find_codex_command() -> str:
    """Windows에 설치된 Codex CLI 실행 파일을 찾는다."""

    codex_command = (
        shutil.which("codex")
        or shutil.which("codex.cmd")
        or shutil.which("codex.exe")
    )

    if codex_command is None:
        raise RuntimeError(
            "Codex CLI를 찾을 수 없습니다.\n"
            "PowerShell에서 다음 명령어를 실행해주세요.\n\n"
            "npm install -g @openai/codex\n"
            "codex login"
        )

    return codex_command


def load_style_examples() -> str:
    """말투 학습용 Markdown 예시를 읽는다."""

    directories = [
        PROJECT_ROOT / "style_examples",
        PROJECT_ROOT / "references" / "style_examples",
        PROJECT_ROOT / "data" / "style_examples",
    ]

    example_paths: list[Path] = []

    for directory in directories:
        if directory.is_dir():
            example_paths.extend(sorted(directory.glob("*.md")))

    single_file_candidates = [
        PROJECT_ROOT / "style_examples.md",
        PROJECT_ROOT / "reference_style.md",
    ]

    for path in single_file_candidates:
        if path.is_file():
            example_paths.append(path)

    # 같은 파일이 여러 후보에서 잡혀도 한 번만 사용한다.
    unique_paths: list[Path] = []
    seen_paths: set[Path] = set()

    for path in example_paths:
        resolved_path = path.resolve()
        if resolved_path not in seen_paths:
            seen_paths.add(resolved_path)
            unique_paths.append(resolved_path)

    if not unique_paths:
        return "말투 예시 파일 없음"

    examples: list[str] = []

    for index, path in enumerate(unique_paths, start=1):
        contents = path.read_text(encoding="utf-8").strip()
        if not contents:
            continue

        examples.append(
            f"[STYLE EXAMPLE {index}: {path.name}]\n"
            f"{contents}"
        )

    return "\n\n".join(examples) if examples else "말투 예시 파일 없음"


def _build_photo_token_map(
    image_paths: Sequence[Path],
) -> dict[str, str]:
    """모델이 실제 파일명을 추측하지 않도록 안정적인 토큰을 만든다."""

    return {
        f"PHOTO_{index:03d}": image_path.name
        for index, image_path in enumerate(image_paths, start=1)
    }


def _format_required_keywords(
    required_keywords: Sequence[Mapping[str, object]],
) -> str:
    if not required_keywords:
        return "- 별도의 체험단 필수 키워드 없음"

    lines: list[str] = []

    for item in required_keywords:
        keyword = str(item.get("keyword", "")).strip()
        if not keyword:
            continue

        title_required = bool(item.get("title_required", False))
        body_min_count = int(item.get("body_min_count", 0))

        lines.append(
            f"- {keyword} | 제목 필수: "
            f"{'예' if title_required else '아니오'} | "
            f"본문 최소 {body_min_count}회"
        )

    return "\n".join(lines) if lines else "- 별도의 체험단 필수 키워드 없음"


def build_blog_prompt(
    image_paths: Sequence[Path],
    restaurant_name: str,
    restaurant_location: str,
    restaurant_points: Sequence[str],
    required_keywords: Sequence[Mapping[str, object]],
    style_profile: Mapping[str, object],
    photo_plan: Mapping[str, object],
) -> str:
    """Codex에 전달할 맛집 블로그 작성 프롬프트를 만든다."""

    points_text = "\n".join(
        f"{index}. {point.strip()}"
        for index, point
        in enumerate(restaurant_points, start=1)
        if point.strip()
    )

    photo_plan_text = _format_photo_plan_for_prompt(
        photo_plan=photo_plan,
        restaurant_points=restaurant_points,
    )

    keyword_text = _format_required_keywords(required_keywords)

    style_text = json.dumps(
        style_profile,
        ensure_ascii=False,
        indent=2,
    )

    style_examples_text = load_style_examples()

    location_text = (
        restaurant_location.strip()
        if restaurant_location.strip()
        else "지역 정보 없음"
    )

    image_count = len(image_paths)

    return f"""
[역할]
너는 실제 방문 자료를 기반으로 네이버 맛집 블로그 글을 작성한다.

첨부 사진과 사용자 입력을 우선 사용하고, 글을 쓰기 전에 음식점을
실시간 웹 검색하여 객관적인 매장 정보를 검증한다.

[음식점]
가게명: {restaurant_name}
지역 또는 주소: {location_text}

[사용자가 직접 입력한 방문 정보]
{points_text}

[체험단 필수 키워드]
{keyword_text}

[확정된 사진 배치 계획]

{photo_plan_text}

[사진 계획 사용 규칙]

- 위 계획은 별도의 사진 분석 단계를 거친 확정 결과다
- 사진을 다시 분석하거나 재분류하지 않는다
- 각 그룹의 marker를 글에 그대로 복사한다
- marker의 PHOTO 토큰, 순서, 묶음과 caption을 변경하지 않는다
- 모든 marker를 계획에 나온 순서대로 정확히 한 번 사용한다
- 서로 다른 그룹을 합치지 않는다
- 하나의 그룹을 여러 그룹으로 나누지 않는다
- allowed_points가 비어 있으면 사진에서 확인되는 내용만 설명한다
- 다른 그룹의 allowed_points를 가져와 사용하지 않는다
- menu_or_price에서는 메뉴판 구성, 가격, 메뉴 종류와
  가독성만 설명한다
- menu_or_price에서 실제 음식의 맛, 식감과 시식 소감을 설명하지 않는다
- place_exterior와 place_interior에서 음식 맛을 설명하지 않는다
- food에서는 해당 사진 속 음식과 연결된 경험만 설명한다
- beverage에서는 해당 음료와 연결된 경험만 설명한다
- cooking_process에서는 조리 방식과 조리 도구만 설명한다
- 사진에서 확인되지 않고 allowed_points에도 없는 사실을 만들지 않는다
- 사용자 입력과 visible_content가 충돌하면 사용자 입력을 우선한다

[사진 설명 작성 규칙]

- visible_content는 사진을 식별하기 위한 참고 정보다
- visible_content 항목을 하나씩 본문 문장으로 옮기지 않는다
- 본문은 allowed_points에 포함된 실제 방문 경험을 중심으로 작성한다
- 사진에 보이는 그릇 색상, 크기, 위치와 사소한 물체를
  불필요하게 설명하지 않는다
- "작은 흰 그릇에 담겨 있었다",
  "소금 알갱이가 보였다",
  "사진에서 윤기가 보였다"와 같은
  단순 관찰 문장을 반복하지 않는다
- allowed_points가 없는 그룹은 객관적인 설명을
  최대 1~2문장만 작성한다
- confidence가 low이면 구체적인 메뉴명, 재료와 소스명을 단정하지 않는다
- confidence가 medium이면 사용자 입력과 명확히 일치하는 내용만 사용한다
- 다른 사진 그룹의 경험과 맛 평가를 가져오지 않는다

[경험과 감상 제한]

사용자가 직접 입력하지 않은 다음 내용을 만들지 않는다.

- 다음에는 다른 메뉴를 먹어보고 싶다는 계획
- 왜 사람들이 주문하는지 알겠다는 평가
- 주문해볼 만하다는 추천
- 입맛이 살아난다는 표현
- 사진을 안 찍을 수 없었다는 표현
- 먹는 흐름이 이어지는 느낌
- 기대감이 올라갔다는 감상
- 가족 외식, 데이트, 회식과 모임 적합성

사용자가 입력한 맛, 식감과 반응만 자연스럽게 표현한다.

[학습한 편집 스타일 프로필]
{style_text}

[실제 블로그 말투 예시]
아래 예시는 사실을 가져오기 위한 자료가 아니다.
장소명, 메뉴명, 가격, 주소와 방문 경험은 절대 새 글에 복사하지 않는다.
말투, 문장 호흡, 줄바꿈, 감탄 방식과 소제목 흐름만 참고한다.

{style_examples_text}

[사실 판단 우선순위]
1. 사용자가 직접 입력한 방문 정보
2. 확정된 사진 계획의 visible_content
3. 웹 검색으로 검증된 객관적인 매장 정보

[웹 검색 규칙]
- 가게명과 지역을 함께 검색한다
- 같은 이름의 다른 가게 또는 다른 지점과 혼동하지 않는다
- 공식 홈페이지, 공식 SNS, 네이버 플레이스 등 공개 매장 정보를 우선한다
- 주소와 전화번호는 신뢰할 수 있는 결과가 일치할 때만 사용한다
- 영업시간, 브레이크타임, 라스트오더와 정기휴무는 각각 별도 항목으로 확인한다
- 출처 간 같은 항목이 충돌하고 최신 정보를 판단할 수 없으면 해당 항목을 생략한다
- 확인되지 않은 지점명, 가격, 주차 방식과 메뉴를 만들지 않는다
- 다른 블로거의 개인적인 경험을 내 경험처럼 사용하지 않는다
- 다른 블로그 문장을 복사하지 않는다
- 웹 검색 URL과 출처 목록은 최종 글에 출력하지 않는다

[전체 분량]
- 정보박스, 사진 마커와 태그를 제외한 본문을 약 2,000~3,000자로 작성한다
- 사진이 8장 이상이면 본문을 최소 2,000자 이상 작성한다
- 주요 음식 하나당 짧은 문단을 3~5개 작성한다
- 사진마다 똑같은 분량과 문장 구조를 반복하지 않는다
- 같은 설명을 표현만 바꿔 여러 구간에서 반복하지 않는다
- 한 사진에서 설명한 핵심 정보는 다른 사진에서 다시 장황하게 설명하지 않는다

[제목]
- 제목은 정확히 한 개만 작성한다
- 제목 후보와 번호를 출력하지 않는다
- 지역, 구체적인 검색 키워드, 가게명, 대표 메뉴와 핵심 특징을 자연스럽게 연결한다
- 예시 블로그의 제목 길이와 단어 배치 방식을 참고한다
- 약 30~55자의 검색형 제목으로 작성한다
- 제목에 이모지와 과도한 특수문자를 사용하지 않는다
- 사용자가 입력하지 않은 가족 외식, 데이트, 혼밥, 회식과 모임을 만들지 않는다
- 가격, 주차, 예약과 웨이팅은 검증됐을 때만 제목에 쓴다
- 사용자가 요구한 필수 제목 키워드는 띄어쓰기까지 그대로 포함한다
- 제목을 Markdown 별표로 감싸지 않는다

[본문 시작 순서]
다음 순서를 다른 규칙보다 우선한다.

# 제목

제목 한 개

# 본문

안녕하세요!

가게명과 지역을 소개하는 짧은 시작 문단

[정보박스]
검증된 매장 정보
[/정보박스]

방문 이유와 주문 메뉴를 소개하는 도입 문단 2~4개

[소제목: 첫 사진 구간에 어울리는 제목]
[구분선]

[사진:PHOTO_001 | 실제 사진 설명]

- 대표사진을 사용하지 않는다
- [대표사진:...]을 출력하지 않는다
- 정보박스보다 앞에는 사진과 스티커를 넣지 않는다
- 정보박스를 음식 설명 중간에 넣지 않는다

[정보박스]
정보박스는 항목명과 콜론을 쓰지 않고 지정된 이모지로 시작한다.
웹 검색으로 확인된 줄만 작성하고 확인되지 않은 줄은 통째로 생략한다.

고정 이모지:
- 상호명: 🏠
- 주소: 📍
- 지역 또는 위치 설명: 📌
- 기본 영업시간: ⏰
- 브레이크타임: ⌚
- 라스트오더: 🕘
- 정기휴무: 📌
- 전화번호: ☎️
- 예약 전화: 📞
- 주차: 🚗

형식:
[정보박스]
🏠 정확한 상호명
📍 검증된 주소
⏰ 검증된 영업시간
⌚ 검증된 브레이크타임
🕘 검증된 라스트오더
📌 검증된 정기휴무
☎️ 검증된 전화번호
🚗 검증된 주차 정보
[/정보박스]

정보박스에서 상호명:, 주소:, 영업시간:, 전화번호: 같은 항목명을 쓰지 않는다.
정보박스에 검색 링크와 URL을 넣지 않는다.

[말투]
- 실제 예시의 말투와 문장 흐름을 스타일 프로필보다 우선 참고한다
- 친구에게 말하듯 자연스럽고 밝은 존댓말을 사용한다
- 한 문단은 1~3문장, 시각적으로 1~4줄로 나눈다
- 정보만 나열하지 않고 사용자가 입력한 반응을 자연스럽게 넣는다
- 짧은 감탄 문단을 글 전체에 4~7개 넣는다
- 메뉴가 등장할 때 기대감과 첫인상을 표현한다
- 같은 종결어미를 연속으로 반복하지 않는다
- 딱딱한 보고서 말투와 상품 설명 말투를 피한다
- 문장 끝에 마침표를 사용하지 않는다

자연스럽게 섞을 수 있는 종결:
~했어요, ~더라구요, ~였어요, ~했답니다, ~괜찮았어요,
~마음에 들었어요, ~인정ㅎㅎ, ~였습니당, ~하고 왔어요

[이모지와 반응]
- 본문에 상황에 맞는 이모지를 6~10개 사용한다
- 같은 이모지를 반복하거나 한 문단에 몰아넣지 않는다
- ㅎㅎ와 ㅋㅋ를 합쳐 3~6회 정도 자연스럽게 사용한다
- 모든 감상 끝에 ㅎㅎ나 ㅋㅋ를 붙이지 않는다
- 정보박스의 고정 항목 이모지는 본문 이모지 개수에서 제외한다

[강조]
- [굵게:내용]을 4~8회 사용한다
- [색상:포인트]내용[/색상]을 2~4회 사용한다
- 메뉴명과 핵심 장점처럼 짧은 구절만 강조한다
- 문장 전체를 굵게 만들지 않는다
- Markdown **내용** 형식을 사용하지 않는다

[소제목, 소소제목과 구분선]
- 본문을 내용에 따라 4~6개의 큰 구간으로 나눈다
- 큰 구간은 반드시 [소제목:내용]으로 시작한다
- 필요할 때만 [소소제목:내용]을 추가한다
- 모든 [구분선] 바로 앞에는 소제목 또는 소소제목이 있어야 한다
- 소제목과 구분선 사이에는 사진, 본문과 스티커를 넣지 않는다
- 소소제목은 소제목과 구분선 사이에만 배치한다
- 마무리를 제외한 모든 [구분선] 바로 다음에는 사진 또는 사진묶음을 배치한다
- 사진이 없는 방문 이유와 주문 메뉴는 정보박스 아래 도입 문단에 포함한다
- 사용할 사진이 없으면 별도의 사진 구간을 만들지 않는다
- 마지막 [마무리] 구간만 사진 없이 작성할 수 있다

소소제목 없는 형식:
[소제목: 기본 상차림]
[구분선]

[사진:PHOTO_001 | 기본 상차림]

소소제목 있는 형식:
[소제목: 카이센동 특]
[소소제목: 두툼한 회와 다양한 해산물]
[구분선]

[사진묶음:PHOTO_002, PHOTO_003 | 전체 모습과 근접 사진]

[스티커]
- [스티커:기대], [스티커:감탄], [스티커:만족], [스티커:마무리]처럼 짧게 쓴다
- 글 전체에서 3~5회 사용한다
- 소제목과 구분선 사이에는 넣지 않는다
- 프로그램이 지원하지 않더라도 마커 형식은 유지한다

[음식 설명]
주요 음식은 사진과 사용자 입력에 근거하여 다음 흐름을 자연스럽게 섞는다.
1. 음식이 나왔을 때 첫인상
2. 사진에서 확인되는 재료와 양
3. 사용자가 입력한 맛과 식감
4. 실제로 곁들여 먹은 방법
5. 가장 좋았던 한입이나 포인트
6. 짧은 개인 반응

목록처럼 기계적으로 쓰지 않는다.
같은 메뉴의 이름, 재료, 두께와 맛을 여러 사진마다 반복하지 않는다.
전체 사진은 구성과 첫인상, 근접 사진은 세부 재료와 식감,
먹는 사진은 조합과 반응처럼 사진별 역할을 나눈다.

[피해야 할 표현]
- 가볍게 참고하기 좋은 후기입니다
- 만족도가 높았던 메뉴
- 입 안에서 존재감을 주는 스타일
- 구성이 만족스러웠어요
- 다시 떠올릴 만한 곳
- 메뉴 선택지가 잘 맞는 곳
- 전체적으로 아쉬웠던 점은
- 여러 재료를 조합해 먹는 재미
- 천천히 먹어야겠다 싶었습니다
- 추천하는 것이 좋겠습니다
- 보는 게 좋겠습니다
- 깔끔하게 정리되는 맛
- 고소한 쪽으로 확 정리되는 맛
- 무난해 보였어요
- 같은 사진 설명 구조의 반복

[마무리]
- 마무리 구간에는 [소제목:마무리]를 사용하지 않습니다
- 음식 소개 뒤 4~6개의 짧은 문단으로 마무리한다
- 주문 메뉴를 전부 다시 나열하지 않는다
- 사용자가 가장 좋았다고 입력한 포인트를 중심으로 끝낸다
- 정보박스 내용을 다시 반복하지 않는다
- 마지막에는 [지도]를 배치한다


[태그]
- [지도] 다음에 [태그]와 [/태그] 마커를 작성한다
- 검색에 적합한 태그를 10~20개 만든다
- 태그는 #을 붙이지 않고 한 줄에 하나씩 작성한다
- 사용자 필수 키워드와 지역, 음식 종류, 가게명을 우선 반영한다
- 확인되지 않은 방문 목적과 시설을 태그로 만들지 않는다

[최종 점검]
출력하기 전에 내부적으로 다음을 확인하고 틀리면 수정한다.
- # 제목과 # 본문이 각각 정확히 있는가
- 제목이 정확히 한 개인가
- 정보박스가 시작 문단 바로 아래에 있는가
- 모든 PHOTO 토큰을 정확히 한 번 사용했는가
- 목록에 없는 PHOTO 토큰을 만들지 않았는가
- 같은 PHOTO 토큰을 중복 사용하지 않았는가
- 모든 구분선 앞에 소제목이 있는가
- 마무리 외 구분선 바로 다음에 사진이 있는가
- 소제목과 구분선 사이에 본문이나 스티커가 없는가
- 기본 상차림을 식사 후 사진으로 잘못 판단하지 않았는가
- 필수 제목 키워드가 제목에 들어갔는가
- 필수 본문 키워드가 최소 횟수를 충족했는가
- 글 내용이 반복되지 않는가
- 검색 URL과 작성 과정을 출력하지 않았는가

[최종 출력]
작성 과정, 검색 출처, 사진 분석표와 점검 결과를 출력하지 않는다.
첫 줄부터 다음 형식으로 제목과 완성된 본문만 출력한다.

# 제목

제목 한 개

# 본문

안녕하세요!

시작 문단

[정보박스]
🏠 정확한 상호명
📍 검증된 주소
⏰ 검증된 영업시간
[/정보박스]

방문 이유와 주문 메뉴 도입 문단

[소제목: 매장 분위기]
[구분선]

[사진:PHOTO_001 | 매장 내부]

본문

[마무리]

사진 없이 작성하는 마무리

[지도]

[태그]
지역 태그
가게명 태그
메뉴 태그
[/태그]
""".strip()


def _normalize_blog_post(blog_post: str) -> str:
    normalized = (
        blog_post
        .replace("\ufeff", "")
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\u2060", "")
        .replace("\xa0", " ")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )

    normalized = re.sub(
        r"\A[ \t]*```(?:markdown|md)?[ \t]*\n",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\n[ \t]*```[ \t]*\Z",
        "",
        normalized,
    )

    # Markdown이 사진 마커의 밑줄과 콜론을 이스케이프한 경우만 복원한다.
    normalized_lines: list[str] = []
    for line in normalized.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("[사진", "[대표사진")):
            line = line.replace("\\_", "_").replace("\\:", ":")
        normalized_lines.append(line)

    return "\n".join(normalized_lines).strip()


def _restore_photo_tokens(
    blog_post: str,
    image_paths: Sequence[Path],
) -> str:
    restored = blog_post

    for token, filename in _build_photo_token_map(image_paths).items():
        restored = restored.replace(token, filename)
        restored = restored.replace(token.replace("_", "\\_"), filename)

    return restored


def _parse_title_and_body(blog_post: str) -> tuple[str, str]:
    normalized = _normalize_blog_post(blog_post)
    headings = list(HEADING_RE.finditer(normalized))

    title_heading = next(
        (match for match in headings if match.group("name") == "제목"),
        None,
    )
    body_heading = next(
        (
            match
            for match in headings
            if match.group("name") == "본문"
            and title_heading is not None
            and match.start() > title_heading.end()
        ),
        None,
    )

    if title_heading is None or body_heading is None:
        preview = normalized[:500]
        raise ValueError(
            "생성된 글에서 '# 제목' 또는 '# 본문' 마커를 찾지 못했습니다.\n\n"
            "생성 결과 앞부분:\n"
            f"{preview}"
        )

    title = normalized[title_heading.end() : body_heading.start()].strip()
    body = normalized[body_heading.end() :].strip()

    if not title:
        raise ValueError("생성된 글의 제목이 비어 있습니다.")
    if not body:
        raise ValueError("생성된 글의 본문이 비어 있습니다.")

    return title, body


def extract_photo_filenames(blog_post: str) -> list[str]:
    """사진 마커에서 파일명만 정확하게 추출한다."""

    filenames: list[str] = []

    for match in PHOTO_MARKER_RE.finditer(blog_post):
        marker_value = match.group("filenames")

        for filename in marker_value.split(","):
            cleaned_filename = (
                filename
                .strip()
                .replace("\\_", "_")
                .replace("\\:", ":")
            )

            if cleaned_filename:
                filenames.append(cleaned_filename)

    return filenames


def _remove_duplicate_only_sections(blog_post: str) -> str:
    """
    중복 사진만 있던 마커가 구분선 직후에 있었다면 해당 구간 전체를 제거한다.

    마커만 지우면 '[구분선] 바로 다음에는 사진' 규칙이 깨지기 때문에
    소제목부터 다음 소제목 직전까지 함께 제거한다.
    """

    lines = blog_post.splitlines()
    remove_indexes: set[int] = set()

    for sentinel_index, line in enumerate(lines):
        if line.strip() != DUPLICATE_PHOTO_SENTINEL:
            continue

        previous_index = sentinel_index - 1
        while previous_index >= 0 and not lines[previous_index].strip():
            previous_index -= 1

        # 구분선 직후가 아니라면 중복 마커 한 줄만 제거한다.
        if (
            previous_index < 0
            or lines[previous_index].strip() != "[구분선]"
        ):
            remove_indexes.add(sentinel_index)
            continue

        section_start = previous_index
        search_index = previous_index - 1

        while search_index >= 0:
            stripped = lines[search_index].strip()

            if stripped.startswith("[소제목:"):
                section_start = search_index
                break

            # 이전 큰 구간까지 넘어가지 않도록 방어한다.
            if stripped == "[구분선]":
                break

            search_index -= 1

        section_end = len(lines)

        for next_index in range(sentinel_index + 1, len(lines)):
            if lines[next_index].strip().startswith("[소제목:"):
                section_end = next_index
                break

        remove_indexes.update(range(section_start, section_end))

    cleaned_lines = [
        line
        for index, line in enumerate(lines)
        if index not in remove_indexes
        and line.strip() != DUPLICATE_PHOTO_SENTINEL
    ]

    cleaned = "\n".join(cleaned_lines)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def normalize_duplicate_photo_markers(blog_post: str) -> str:
    """
    같은 사진의 첫 사용은 유지하고 두 번째 이후 참조를 자동 제거한다.

    사진묶음 안에서 일부만 중복이면 중복 파일명만 제거하고,
    전부 중복이면 마커 또는 중복 전용 소제목 구간을 제거한다.
    """

    seen_filenames: set[str] = set()

    def replace_marker(match: re.Match[str]) -> str:
        marker_filenames = [
            filename.strip().replace("\\_", "_").replace("\\:", ":")
            for filename in match.group("filenames").split(",")
            if filename.strip()
        ]

        remaining_filenames: list[str] = []

        for filename in marker_filenames:
            if filename in seen_filenames:
                continue

            seen_filenames.add(filename)
            remaining_filenames.append(filename)

        if not remaining_filenames:
            return DUPLICATE_PHOTO_SENTINEL

        marker_kind = (
            "사진"
            if len(remaining_filenames) == 1
            else "사진묶음"
        )
        raw_description = match.group("description") or ""
        description = ""

        if raw_description:
            description_text = raw_description.split("|", 1)[1].strip()
            if description_text:
                description = f" | {description_text}"

        return (
            f"[{marker_kind}:"
            f"{', '.join(remaining_filenames)}"
            f"{description}]"
        )

    normalized = PHOTO_MARKER_FULL_RE.sub(
        replace_marker,
        blog_post,
    )

    return _remove_duplicate_only_sections(normalized)


def validate_photo_filenames(
    blog_post: str,
    image_paths: Sequence[Path],
) -> None:
    """누락, 존재하지 않는 이름과 중복 사진을 모두 검사한다."""

    available_filenames = {
        image_path.name
        for image_path in image_paths
    }

    referenced_filenames = extract_photo_filenames(blog_post)
    referenced_set = set(referenced_filenames)
    filename_counts = Counter(referenced_filenames)

    missing_filenames = sorted(
        available_filenames - referenced_set
    )
    unknown_filenames = sorted(
        referenced_set - available_filenames
    )
    duplicate_filenames = sorted(
        filename
        for filename, count in filename_counts.items()
        if count > 1
    )

    errors: list[str] = []

    if missing_filenames:
        errors.append(
            "본문에서 사용하지 않은 실제 이미지:\n"
            + "\n".join(f"- {name}" for name in missing_filenames)
        )

    if unknown_filenames:
        errors.append(
            "img 폴더에 존재하지 않는 이미지:\n"
            + "\n".join(f"- {name}" for name in unknown_filenames)
        )

    if duplicate_filenames:
        errors.append(
            "본문에서 중복 사용한 이미지:\n"
            + "\n".join(f"- {name}" for name in duplicate_filenames)
        )

    if errors:
        raise ValueError(
            "생성된 글의 이미지 파일명 검증에 실패했습니다.\n\n"
            + "\n\n".join(errors)
        )


def validate_required_keywords(
    blog_post: str,
    required_keywords: Sequence[Mapping[str, object]],
) -> None:
    """체험단 제목·본문 필수 키워드를 검증한다."""

    title, body = _parse_title_and_body(blog_post)

    tag_start = TAG_START_RE.search(body)
    body_without_tags = (
        body[: tag_start.start()].rstrip()
        if tag_start is not None
        else body
    )

    errors: list[str] = []

    for item in required_keywords:
        keyword = str(item.get("keyword", "")).strip()
        if not keyword:
            continue

        title_required = bool(item.get("title_required", False))
        body_min_count = int(item.get("body_min_count", 0))

        title_count = title.count(keyword)
        body_count = body_without_tags.count(keyword)

        if title_required and title_count < 1:
            errors.append(
                f"제목 필수 키워드 누락: {keyword}"
            )

        if body_count < body_min_count:
            errors.append(
                f"본문 키워드 횟수 부족: {keyword} "
                f"({body_count}/{body_min_count}회)"
            )

    if errors:
        raise ValueError(
            "생성된 글이 체험단 필수 키워드 조건을 충족하지 못했습니다.\n\n"
            + "\n".join(f"- {error}" for error in errors)
        )


def validate_heading_dividers(blog_post: str) -> None:
    """소제목·구분선·사진 배치 규칙을 검사한다."""

    lines = blog_post.splitlines()

    for index, raw_line in enumerate(lines):
        if raw_line.strip() != "[구분선]":
            continue

        previous_index = index - 1
        while previous_index >= 0 and not lines[previous_index].strip():
            previous_index -= 1

        if previous_index < 0:
            raise ValueError("소제목 없이 사용된 [구분선]이 있습니다.")

        previous_line = lines[previous_index].strip()
        if not previous_line.startswith(("[소제목:", "[소소제목:")):
            raise ValueError(
                "모든 [구분선] 바로 앞에는 [소제목:...] 또는 "
                "[소소제목:...]이 있어야 합니다."
            )

        # 이 구분선에 연결된 큰 소제목을 찾는다.
        heading_text = previous_line
        search_index = previous_index
        while search_index >= 0:
            candidate = lines[search_index].strip()
            if candidate.startswith("[소제목:"):
                heading_text = candidate
                break
            search_index -= 1

        is_closing_section = "마무리" in heading_text
        if is_closing_section:
            continue

        next_index = index + 1
        while next_index < len(lines) and not lines[next_index].strip():
            next_index += 1

        if next_index >= len(lines):
            raise ValueError("[구분선] 뒤의 사진과 본문이 비어 있습니다.")

        next_line = lines[next_index].strip()
        if not next_line.startswith(("[사진:", "[사진묶음:")):
            raise ValueError(
                "마무리를 제외한 [구분선] 바로 다음에는 "
                "[사진:...] 또는 [사진묶음:...]이 있어야 합니다."
            )


def validate_blog_post(
    blog_post: str,
    image_paths: Sequence[Path],
    required_keywords: Sequence[Mapping[str, object]],
) -> None:
    """생성 결과의 핵심 구조를 한 번에 검증한다."""

    _parse_title_and_body(blog_post)

    if "[대표사진:" in blog_post:
        raise ValueError("대표사진 마커를 사용하면 안 됩니다.")

    validate_photo_filenames(
        blog_post=blog_post,
        image_paths=image_paths,
    )
    validate_required_keywords(
        blog_post=blog_post,
        required_keywords=required_keywords,
    )
    validate_heading_dividers(blog_post)


def _build_codex_command(
    codex_command: str,
    image_paths: Sequence[Path],
    *,
    use_search: bool = True,
    output_schema_path: Path | None = None,
) -> list[str]:
    command = [codex_command]

    if use_search:
        command.append("--search")

    command.extend(
        [
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--cd",
            str(PROJECT_ROOT),
        ]
    )

    if output_schema_path is not None:
        command.extend(
            [
                "--output-schema",
                str(output_schema_path),
            ]
        )

    for image_path in image_paths:
        command.extend(
            [
                "--image",
                str(image_path),
            ]
        )

    command.append("-")

    return command

def _run_codex(
    codex_command: str,
    image_paths: Sequence[Path],
    prompt: str,
    *,
    use_search: bool = True,
    output_schema_path: Path | None = None,
) -> str:
    resolved_schema_path: Path | None = None

    if output_schema_path is not None:
        resolved_schema_path = (
            output_schema_path.resolve()
        )

        if not resolved_schema_path.is_file():
            raise FileNotFoundError(
                "Codex 출력 스키마 파일을 찾을 수 없습니다.\n"
                f"확인할 경로: {resolved_schema_path}"
            )

    command = _build_codex_command(
        codex_command=codex_command,
        image_paths=image_paths,
        use_search=use_search,
        output_schema_path=resolved_schema_path,
    )

    creation_flags = getattr(
        subprocess,
        "CREATE_NO_WINDOW",
        0,
    )

    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CODEX_TIMEOUT_SECONDS,
            check=False,
            creationflags=creation_flags,
        )

    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            "Codex 작업 시간이 "
            f"{CODEX_TIMEOUT_SECONDS // 60}분을 초과했습니다."
        ) from error

    except OSError as error:
        raise RuntimeError(
            "Codex CLI를 실행하지 못했습니다.\n"
            f"{error}"
        ) from error

    if result.returncode != 0:
        error_message = result.stderr.strip()

        if not error_message:
            error_message = (
                "Codex CLI에서 상세 오류를 "
                "반환하지 않았습니다."
            )

        raise RuntimeError(
            "Codex 작업에 실패했습니다.\n\n"
            f"{error_message}"
        )

    output = result.stdout.strip()

    if not output:
        raise RuntimeError(
            "Codex가 빈 결과를 반환했습니다."
        )

    return output

def _save_photo_plan(
    photo_plan: Mapping[str, object],
) -> Path:
    """사진 분석 결과를 디버그 파일로 저장한다."""

    debug_dir = (
        PROJECT_ROOT
        / "output"
        / "debug"
    )

    debug_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = (
        debug_dir
        / "photo_plan.json"
    )

    output_path.write_text(
        json.dumps(
            photo_plan,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return output_path


def analyze_photos(
    codex_command: str,
    image_paths: Sequence[Path],
    restaurant_points: Sequence[str],
) -> dict[str, object]:
    """
    Codex로 사진을 분석한 뒤
    검증된 사진 배치 계획을 반환한다.
    """

    if not PHOTO_ANALYSIS_SCHEMA_PATH.is_file():
        raise FileNotFoundError(
            "사진 분석 스키마를 찾을 수 없습니다.\n"
            f"확인할 경로: {PHOTO_ANALYSIS_SCHEMA_PATH}"
        )

    original_prompt = build_photo_analysis_prompt(
        image_paths=image_paths,
        restaurant_points=restaurant_points,
    )

    current_prompt = original_prompt
    last_error: Exception | None = None

    for attempt in range(
        1,
        MAX_PHOTO_ANALYSIS_ATTEMPTS + 1,
    ):
        print(
            "사진 분석 중 "
            f"({attempt}/"
            f"{MAX_PHOTO_ANALYSIS_ATTEMPTS})"
        )

        raw_result = _run_codex(
            codex_command=codex_command,
            image_paths=image_paths,
            prompt=current_prompt,
            use_search=False,
            output_schema_path=(
                PHOTO_ANALYSIS_SCHEMA_PATH
            ),
        )

        try:
            parsed_result = json.loads(
                raw_result
            )

            if not isinstance(
                parsed_result,
                dict,
            ):
                raise ValueError(
                    "사진 분석 결과가 "
                    "JSON 객체가 아닙니다."
                )

            validate_photo_plan(
                photo_plan=parsed_result,
                image_paths=image_paths,
                restaurant_points=restaurant_points,
            )

            saved_path = _save_photo_plan(
                parsed_result
            )

            print(
                "사진 분석 결과 저장 완료: "
                f"{saved_path}"
            )

            return parsed_result

        except (
            json.JSONDecodeError,
            ValueError,
        ) as error:
            last_error = error

            debug_dir = (
                PROJECT_ROOT
                / "output"
                / "debug"
            )

            debug_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            invalid_result_path = (
                debug_dir
                / (
                    "invalid_photo_plan_"
                    f"{attempt}.txt"
                )
            )

            invalid_error_path = (
                debug_dir
                / (
                    "invalid_photo_plan_"
                    f"{attempt}_error.txt"
                )
            )

            invalid_result_path.write_text(
                raw_result,
                encoding="utf-8",
            )

            invalid_error_path.write_text(
                str(error),
                encoding="utf-8",
            )

            if (
                attempt
                >= MAX_PHOTO_ANALYSIS_ATTEMPTS
            ):
                break

            current_prompt = f"""
{original_prompt}

[이전 사진 분석 검증 실패]

이전 결과에는 다음 오류가 있다.

{error}

[재분석 지시]

- 모든 PHOTO 토큰을 정확히 한 번 사용한다
- PHOTO 토큰을 누락하거나 중복 사용하지 않는다
- 존재하지 않는 토큰을 만들지 않는다
- 관련 없는 사진을 같은 그룹에 넣지 않는다
- 사용자 포인트 번호를 정확하게 사용한다
- 수정된 전체 JSON 결과를 다시 반환한다
""".strip()

    raise RuntimeError(
        "사진 분석 결과가 검증을 "
        "통과하지 못했습니다.\n\n"
        f"마지막 오류:\n{last_error}\n\n"
        "검증 실패 결과는 "
        "output/debug 폴더에서 확인할 수 있습니다."
    )

def _build_retry_prompt(
    original_prompt: str,
    invalid_blog_post: str,
    validation_error: Exception,
) -> str:
    return f"""
{original_prompt}

[이전 출력 검증 실패]
이전 출력은 아래 검증 오류 때문에 사용할 수 없다.

{validation_error}

[재작성 지시]
- 오류가 발생한 부분만 설명하지 말고 완성된 전체 글을 다시 출력한다
- 이전 글의 장점과 사실 정보는 유지하되 검증 오류를 모두 수정한다
- 사진 마커에는 제공된 PHOTO 토큰만 사용한다
- 최종 출력 형식 외의 사과, 설명과 점검 결과를 출력하지 않는다

[이전 출력]
{invalid_blog_post}
""".strip()


def _save_invalid_draft(
    attempt: int,
    blog_post: str,
    validation_error: Exception,
) -> None:
    """최종 실패 원인을 확인할 수 있도록 검증 실패 초안을 보관한다."""

    debug_dir = PROJECT_ROOT / "output" / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    draft_path = debug_dir / f"invalid_blog_post_attempt_{attempt}.md"
    error_path = debug_dir / f"invalid_blog_post_attempt_{attempt}.txt"

    draft_path.write_text(blog_post, encoding="utf-8")
    error_path.write_text(str(validation_error), encoding="utf-8")


def generate_blog_post(
    image_paths: Sequence[Path],
    restaurant_name: str,
    restaurant_location: str,
    restaurant_points: Sequence[str],
    required_keywords: Sequence[Mapping[str, object]],
    style_profile: Mapping[str, object],
) -> str:
    """
    Codex CLI로 음식점 정보를 웹 검색하고 첨부 사진을 분석해
    네이버 맛집 블로그 글을 생성한다.
    """

    if not image_paths:
        raise ValueError("블로그 글을 생성할 이미지가 없습니다.")

    if not restaurant_name.strip():
        raise ValueError("가게명이 입력되지 않았습니다.")

    cleaned_points = [
        point.strip()
        for point in restaurant_points
        if point.strip()
    ]
    if not cleaned_points:
        raise ValueError("맛집 포인트가 입력되지 않았습니다.")

    resolved_image_paths: list[Path] = []
    seen_filenames: set[str] = set()

    for image_path in image_paths:
        resolved_path = image_path.resolve()

        if not resolved_path.is_file():
            raise FileNotFoundError(
                f"이미지 파일을 찾을 수 없습니다: {resolved_path}"
            )

        if resolved_path.name in seen_filenames:
            raise ValueError(
                "서로 다른 경로에 같은 파일명의 이미지가 있습니다: "
                f"{resolved_path.name}"
            )

        seen_filenames.add(resolved_path.name)
        resolved_image_paths.append(resolved_path)

    codex_command = find_codex_command()

    print("첨부 사진을 분석하고 있습니다.")

    photo_plan = analyze_photos(
        codex_command=codex_command,
        image_paths=resolved_image_paths,
        restaurant_points=cleaned_points,
    )

    print("사진 분석과 묶음 구성이 완료되었습니다.")

    original_prompt = build_blog_prompt(
        image_paths=resolved_image_paths,
        restaurant_name=restaurant_name.strip(),
        restaurant_location=restaurant_location.strip(),
        restaurant_points=cleaned_points,
        required_keywords=required_keywords,
        style_profile=style_profile,
        photo_plan=photo_plan,
    )

    current_prompt = original_prompt
    last_error: Exception | None = None

    for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
        raw_blog_post = _run_codex(
            codex_command=codex_command,

            # 사진 분석은 이미 끝났으므로
            # 글 작성 단계에는 이미지를 다시 전달하지 않는다.
            image_paths=(),

            prompt=current_prompt,
            use_search=True,
        )

        normalized_blog_post = _normalize_blog_post(raw_blog_post)
        blog_post = _restore_photo_tokens(
            blog_post=normalized_blog_post,
            image_paths=resolved_image_paths,
        )

        # 모델이 같은 사진을 두 번 출력한 경우 재생성 전에 결정적으로 정리한다.
        # 존재하지 않는 사진과 누락 사진은 아래 검증에서 계속 오류 처리한다.
        blog_post = normalize_duplicate_photo_markers(blog_post)

        try:
            validate_blog_post(
                blog_post=blog_post,
                image_paths=resolved_image_paths,
                required_keywords=required_keywords,
            )
            return blog_post

        except ValueError as error:
            last_error = error
            _save_invalid_draft(
                attempt=attempt,
                blog_post=blog_post,
                validation_error=error,
            )

            if attempt >= MAX_GENERATION_ATTEMPTS:
                break

            current_prompt = _build_retry_prompt(
                original_prompt=original_prompt,
                invalid_blog_post=raw_blog_post,
                validation_error=error,
            )

    raise RuntimeError(
        f"Codex가 {MAX_GENERATION_ATTEMPTS}번 생성했지만 "
        "출력 검증을 통과하지 못했습니다.\n\n"
        f"마지막 오류:\n{last_error}\n\n"
        "검증 실패 초안은 output/debug 폴더에서 확인할 수 있습니다."
    )


def build_photo_analysis_prompt(
    image_paths: Sequence[Path],
    restaurant_points: Sequence[str],
) -> str:
    """
    첨부 사진을 분석하고 블로그 작성에 사용할
    사진 묶음 계획을 JSON으로 생성한다.

    음식점 종류나 특정 메뉴에 종속되지 않는
    범용 분석 규칙을 사용한다.
    """

    photo_token_map = _build_photo_token_map(
        image_paths
    )

    photo_text = "\n".join(
        f"- {token} = {filename}"
        for token, filename
        in photo_token_map.items()
    )

    point_text = "\n".join(
        f"{index}. {point.strip()}"
        for index, point
        in enumerate(restaurant_points, start=1)
        if point.strip()
    )

    if not point_text:
        point_text = "사용자 입력 포인트 없음"

    return f"""
[역할]
너는 실제 방문 자료를 기반으로 네이버 맛집 블로그 글을 작성한다.

사용자 입력과 확정된 사진 분석 계획을 우선 사용하고,
글을 쓰기 전에 음식점을 실시간 웹 검색하여
객관적인 매장 정보를 검증한다.

[첨부 사진 개수]

총 {len(image_paths)}개

[사진 토큰과 실제 파일명]

{photo_text}

[사용자가 직접 입력한 방문 정보]

{point_text}

[분류 카테고리]

각 사진 묶음에는 아래 카테고리 중 하나를 사용한다.

- place_exterior
  매장 외관, 간판, 입구, 건물 외부

- place_interior
  좌석, 테이블, 인테리어, 매장 내부 분위기

- facility_or_service
  주차장, 화장실, 셀프바, 대기 공간,
  키오스크, 호출벨과 편의시설

- notice_or_information
  영업 안내, 이용 안내, 행사 안내,
  원산지 표시와 공지문

- menu_or_price
  메뉴판, 가격표, 주문 화면과 메뉴 안내

- table_setting
  음식이 나오기 전 테이블 전체 모습,
  식기와 기본 상차림

- side_dish
  기본 반찬, 밑반찬, 빵, 피클,
  기본 제공 음식

- food
  실제 주문한 음식, 메인 메뉴,
  추가 메뉴와 음식 근접 사진

- beverage
  실제 제공되거나 주문한 음료와 주류

- cooking_process
  굽기, 끓이기, 자르기, 조리 장면,
  불판과 조리 기계 사용 모습

- eating_process
  음식을 집거나 먹는 장면,
  소스를 찍거나 섞는 장면

- sauce_or_condiment
  소스, 양념, 향신료와 조미료

- dessert
  디저트, 후식, 아이스크림과 과일

- receipt_or_payment
  영수증, 결제 화면과 주문 내역

- other
  위 분류에 해당하지 않거나
  사진 내용을 확실하게 판단하기 어려운 경우

[사진 판독 규칙]

1. 실제 사진에서 명확하게 보이는 내용만 판단한다.

2. 파일명, 촬영 순서와 사용자 입력만 보고
   사진 내용을 추측하지 않는다.

3. 음식 이름을 확실하게 구분할 수 없으면
   구체적인 메뉴명을 만들지 않는다.

4. 사진에서 맛, 향, 식감과 만족도는
   직접 확인할 수 없으므로 추측하지 않는다.

5. 사진 속 글씨를 정확하게 읽을 수 없으면
   메뉴명, 가격과 안내 내용을 임의로 작성하지 않는다.

6. 비슷하게 생긴 음식이라도 확실하지 않으면
   "구이 메뉴", "면 요리", "음료"처럼
   일반적인 표현을 사용한다.

7. 사용자가 직접 입력한 정보는 사실 자료로만 사용한다.
   사용자 입력을 사진에 억지로 연결하지 않는다.

8. 사진 추정과 사용자 입력이 충돌할 경우
   사용자 입력을 우선한다.

[사진 중심 대상 판독]

1. 사진에서 가장 크고 중앙에 위치한 대상을
   primary_subject로 정한다.

2. 소스나 반찬이 사진 일부에 보이더라도
   고기, 면, 음료와 같은 주문 메뉴가 중심이면
   주문 메뉴를 primary_subject로 정한다.

3. 음식이나 고기를 젓가락, 집게 또는 꼬치로
   들고 있는 사진은 sauce_or_condiment로 분류하지 않는다.

4. 소스 그릇이나 양념통 자체가 사진의 중심이고
   음식이 중심에 없을 때만 sauce_or_condiment로 분류한다.

5. 고기를 소스에 찍는 사진은
   sauce_or_condiment가 아니라 eating_process로 분류한다.

6. 불판 위에 음식이 올라가 있으면 cooking_process로,
   다 익은 음식을 접시나 젓가락으로 보여주면 food 또는
   eating_process로 분류한다.

7. 중심 대상을 확신할 수 없으면 confidence를 low로 설정하고
   구체적인 음식 이름을 사용하지 않는다.

8. 사용자 입력만으로 음식 종류를 확정하지 않는다.
   다만 사진과 사용자 입력이 함께 일치하면
   구체적인 메뉴명을 사용할 수 있다.

[식사 진행 순서]

각 그룹에는 narrative_stage와 narrative_order를 지정한다.

다음 의존 관계를 반드시 지킨다.

- 매장 외관은 매장 내부보다 먼저 배치한다
- 메뉴판은 실제 음식 소개보다 먼저 배치한다
- 기본 상차림은 메인 음식보다 먼저 배치한다
- 숯불, 불판과 조리 도구 준비는 굽는 장면보다 먼저 배치한다
- 생고기와 조리 전 음식은 굽는 장면보다 먼저 배치한다
- 굽는 장면은 다 익은 음식보다 먼저 배치한다
- 다 익은 음식은 먹거나 소스에 찍는 장면보다 먼저 배치한다
- 같은 음식의 사진은 가능한 한 서로 가까이 배치한다
- narrative_order는 1부터 그룹 수까지 중복 없이 지정한다   

[사진 묶음 규칙]

1. 모든 PHOTO 토큰을 정확히 한 번 사용한다.

2. 같은 PHOTO 토큰을 여러 그룹에 중복 사용하지 않는다.

3. 서로 같은 대상이나 같은 장면을 찍은 사진만 묶는다.

4. 카테고리가 같더라도 서로 다른 음식이나
   서로 다른 대상을 촬영했다면 별도 그룹으로 나눈다.

5. 같은 음식의 전체 모습, 근접 사진과 다른 각도 사진은
   하나의 그룹으로 묶을 수 있다.

6. 같은 조리 과정을 연속으로 촬영한 JPG와 GIF는
   하나의 그룹으로 묶을 수 있다.

7. 서로 관련 없는 음식, 음료, 메뉴판과 매장 사진을
   한 그룹에 섞지 않는다.

8. 여러 장의 메뉴판 사진은 메뉴판 그룹으로 묶되,
   실제 주문 음식이나 음료 사진을 함께 넣지 않는다.

9. 기본 반찬 사진은 메인 음식 사진과 분리한다.

10. 소스나 양념만 촬영한 사진은 음식 사진과 구분하되,
    해당 음식을 찍어 먹는 장면이라면
    eating_process로 묶을 수 있다.

11. 사진이 한 장만 독립적인 내용을 보여주면
    한 장짜리 그룹으로 둔다.

12. 사진이 많다는 이유만으로 관련 없는 사진을
    억지로 묶지 않는다.

[사용자 포인트 연결 규칙]

각 그룹의 allowed_point_indexes에는
그 사진 묶음 바로 아래에서 사용해도 되는
사용자 포인트 번호만 넣는다.

1. 사용자 포인트의 대상과 사진 속 대상이
   명확하게 일치할 때만 연결한다.

2. 관련된 사용자 포인트가 없으면
   빈 배열을 사용한다.

3. 하나의 사용자 포인트가 여러 사진 그룹과
   명확하게 관련되면 여러 그룹에 연결할 수 있다.

4. menu_or_price 그룹에는 다음 내용만 연결한다.

   - 메뉴판 형태
   - 메뉴 종류
   - 가격
   - 메뉴판 가독성
   - 주문 방식
   - 선택지가 다양하다는 정보

5. menu_or_price 그룹에는 다음 내용을 연결하지 않는다.

   - 실제 주문한 음식의 맛
   - 음식의 식감
   - 실제 먹어본 소감
   - 음료나 주류의 맛
   - 음식과 음료의 조합

6. place_exterior와 place_interior 그룹에는
   매장 위치, 분위기, 청결도와 좌석 관련 정보만 연결한다.

7. facility_or_service 그룹에는
   주차, 화장실, 셀프바, 주문 방식과
   직원 서비스 관련 정보만 연결한다.

8. side_dish 그룹에는
   기본 반찬과 기본 제공 음식 관련 정보만 연결한다.

9. food 그룹에는 사진 속 음식과 동일한 메뉴의
   맛, 식감, 재료와 양에 관한 정보만 연결한다.

10. beverage 그룹에는 사진 속 음료와 동일한 대상의
    맛과 음식을 함께 먹은 경험만 연결한다.

11. cooking_process 그룹에는
    굽는 방식, 조리 방법, 불판과 조리 도구에
    관한 정보만 연결한다.

12. sauce_or_condiment 그룹에는
    소스, 양념, 향신료와 찍어 먹는 방법에 관한
    정보만 연결한다.

13. 사진 속 대상을 확실하게 판단할 수 없다면
    사용자 포인트를 연결하지 않는다.

[caption 작성 규칙]

- caption은 사진에서 보이는 대상을 짧게 설명한다.
- 맛이나 개인적인 감상은 caption에 넣지 않는다.
- 확실하지 않은 음식 이름을 만들지 않는다.
- "맛있는 음식", "만족스러운 메뉴" 같은
  주관적인 표현을 사용하지 않는다.
- 사진 묶음 전체를 대표할 수 있는 표현을 사용한다.

올바른 caption 예시:

- 매장 외관과 입구
- 매장 내부와 테이블
- 책자 형태의 메뉴판
- 기본 반찬 구성
- 주문한 구이 메뉴
- 불판에서 익어가는 음식
- 주문한 음료
- 소스와 테이블 양념

[visible_content 작성 규칙]

- 사진에서 직접 확인되는 시각 정보만 작성한다.
- 한 그룹당 핵심 내용 1~4개만 작성한다.
- 맛, 향, 식감과 방문자의 기분을 작성하지 않는다.
- 메뉴명이나 재료가 불확실하면 일반적인 표현을 사용한다.

[그룹 배치 순서]

블로그에서 자연스럽게 사용할 수 있도록
가능하면 다음 흐름으로 그룹을 정렬한다.

1. place_exterior
2. place_interior
3. facility_or_service
4. notice_or_information
5. menu_or_price
6. table_setting
7. side_dish
8. food
9. cooking_process
10. eating_process
11. sauce_or_condiment
12. beverage
13. dessert
14. receipt_or_payment
15. other

해당 사진이 없는 단계는 만들지 않는다.
촬영 순서를 그대로 따를 필요는 없다.

[최종 점검]

JSON을 반환하기 전에 내부적으로 확인한다.

- 모든 PHOTO 토큰을 정확히 한 번 사용했는가
- 중복 사용한 PHOTO 토큰이 없는가
- 목록에 없는 PHOTO 토큰을 만들지 않았는가
- 서로 관련 없는 사진이 한 그룹에 섞이지 않았는가
- 메뉴판 사진에 음식 맛 포인트를 연결하지 않았는가
- 사진과 사용자 포인트의 대상이 정확히 일치하는가
- 불확실한 사진에 구체적인 메뉴명을 만들지 않았는가

[출력 형식]

설명, Markdown과 코드 블록을 출력하지 않는다.
지정된 JSON Schema에 맞는 JSON만 반환한다.
""".strip()

def validate_photo_plan(
    photo_plan: Mapping[str, object],
    image_paths: Sequence[Path],
    restaurant_points: Sequence[str],
) -> None:
    """사진 분석 결과의 토큰, 분류와 포인트 번호를 검증한다."""

    groups = photo_plan.get("groups")

    if not isinstance(groups, list):
        raise ValueError(
            "사진 분석 결과에 groups 배열이 없습니다."
        )

    if not groups:
        raise ValueError(
            "사진 분석 결과에 사진 그룹이 없습니다."
        )

    expected_tokens = set(
        _build_photo_token_map(image_paths)
    )

    used_tokens: list[str] = []
    errors: list[str] = []

    for group_index, group in enumerate(
        groups,
        start=1,
    ):
        if not isinstance(group, dict):
            errors.append(
                f"{group_index}번째 사진 그룹이 "
                "객체 형식이 아닙니다."
            )
            continue

        category = group.get("category")

        if category not in PHOTO_ANALYSIS_CATEGORIES:
            errors.append(
                f"{group_index}번째 그룹의 "
                f"카테고리가 잘못되었습니다: {category}"
            )

        photo_tokens = group.get("photo_tokens")

        if not isinstance(photo_tokens, list):
            errors.append(
                f"{group_index}번째 그룹의 "
                "photo_tokens가 배열이 아닙니다."
            )
            continue

        if not photo_tokens:
            errors.append(
                f"{group_index}번째 그룹에 "
                "사진 토큰이 없습니다."
            )

        for token in photo_tokens:
            if not isinstance(token, str):
                errors.append(
                    f"{group_index}번째 그룹에 "
                    "문자열이 아닌 토큰이 있습니다."
                )
                continue

            used_tokens.append(token)

        caption = group.get("caption")

        if (
            not isinstance(caption, str)
            or not caption.strip()
        ):
            errors.append(
                f"{group_index}번째 그룹의 "
                "caption이 비어 있습니다."
            )

        visible_content = group.get(
            "visible_content"
        )

        if not isinstance(visible_content, list):
            errors.append(
                f"{group_index}번째 그룹의 "
                "visible_content가 배열이 아닙니다."
            )
        else:
            for content in visible_content:
                if not isinstance(content, str):
                    errors.append(
                        f"{group_index}번째 그룹의 "
                        "visible_content에 문자열이 아닌 "
                        "값이 있습니다."
                    )

        point_indexes = group.get(
            "allowed_point_indexes"
        )

        if not isinstance(point_indexes, list):
            errors.append(
                f"{group_index}번째 그룹의 "
                "allowed_point_indexes가 배열이 아닙니다."
            )
        else:
            for point_index in point_indexes:
                # bool은 int의 하위 타입이므로 type으로 검사
                if type(point_index) is not int:
                    errors.append(
                        f"{group_index}번째 그룹에 "
                        "정수가 아닌 포인트 번호가 있습니다."
                    )
                    continue

                if not (
                    1
                    <= point_index
                    <= len(restaurant_points)
                ):
                    errors.append(
                        f"{group_index}번째 그룹에 "
                        "존재하지 않는 포인트 번호가 있습니다: "
                        f"{point_index}"
                    )

    token_counts = Counter(used_tokens)

    duplicated_tokens = sorted(
        token
        for token, count in token_counts.items()
        if count > 1
    )

    used_token_set = set(used_tokens)

    missing_tokens = sorted(
        expected_tokens - used_token_set
    )

    unknown_tokens = sorted(
        used_token_set - expected_tokens
    )

    if duplicated_tokens:
        errors.append(
            "중복 배치된 사진 토큰: "
            + ", ".join(duplicated_tokens)
        )

    if missing_tokens:
        errors.append(
            "누락된 사진 토큰: "
            + ", ".join(missing_tokens)
        )

    if unknown_tokens:
        errors.append(
            "존재하지 않는 사진 토큰: "
            + ", ".join(unknown_tokens)
        )

    if errors:
        raise ValueError(
            "사진 분석 결과 검증에 실패했습니다.\n\n"
            + "\n".join(
                f"- {error}"
                for error in errors
            )
        )

def _format_photo_plan_for_prompt(
    photo_plan: Mapping[str, object],
    restaurant_points: Sequence[str],
) -> str:
    """
    사진 분석 결과에 실제 사용자 포인트 내용과
    그대로 사용할 사진 마커를 추가한다.
    """

    raw_groups = photo_plan.get("groups")

    if not isinstance(raw_groups, list):
        raise ValueError(
            "사진 분석 결과의 groups가 올바르지 않습니다."
        )

    formatted_groups: list[dict[str, object]] = []

    for group in raw_groups:
        if not isinstance(group, dict):
            continue

        raw_tokens = group.get(
            "photo_tokens",
            [],
        )

        photo_tokens = [
            str(token)
            for token in raw_tokens
        ]

        caption = str(
            group.get("caption", "")
        ).strip()

        marker_kind = (
            "사진"
            if len(photo_tokens) == 1
            else "사진묶음"
        )

        marker = (
            f"[{marker_kind}:"
            f"{', '.join(photo_tokens)}"
            f" | {caption}]"
        )

        raw_point_indexes = group.get(
            "allowed_point_indexes",
            [],
        )

        allowed_points = [
            restaurant_points[index - 1]
            for index in raw_point_indexes
            if (
                type(index) is int
                and 1
                <= index
                <= len(restaurant_points)
            )
        ]

        confidence = str(
            group.get(
                "confidence",
                "low",
            )
        )

        # 사진 대상을 확실하게 판독하지 못했으면
        # 특정 음식의 맛이나 경험을 연결하지 않는다.
        if confidence == "low":
            allowed_points = []

        formatted_groups.append(
            {
                "marker": marker,
                "category": group.get("category"),
                "primary_subject": group.get(
                    "primary_subject"
                ),
                "narrative_stage": group.get(
                    "narrative_stage"
                ),
                "narrative_order": group.get(
                    "narrative_order"
                ),
                "confidence": confidence,
                "caption": caption,
                "visible_content": group.get(
                    "visible_content",
                    [],
                ),
                "allowed_points": allowed_points,
            }
        )

        formatted_groups.sort(
            key=lambda group: int(
                group.get(
                    "narrative_order",
                    999,
                )
            )
        )

    return json.dumps(
        {
            "groups": formatted_groups,
        },
        ensure_ascii=False,
        indent=2,
    )