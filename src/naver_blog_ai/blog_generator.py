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
) -> str:
    """Codex에 전달할 맛집 블로그 작성 프롬프트를 만든다."""

    photo_token_map = _build_photo_token_map(image_paths)

    image_text = "\n".join(
        f"- {token} = {filename}"
        for token, filename in photo_token_map.items()
    )

    points_text = "\n".join(
        f"- {point.strip()}"
        for point in restaurant_points
        if point.strip()
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

[첨부 사진 개수]
총 {image_count}개

[첨부 사진 토큰과 실제 파일명]
{image_text}

[사진 토큰 사용 규칙]
- 사진 마커에는 실제 파일명 대신 위 목록의 PHOTO_001 형식 토큰을 사용한다
- 최종 프로그램이 사진 토큰을 실제 파일명으로 자동 변환한다
- 위 목록에 없는 PHOTO 토큰을 만들지 않는다
- PHOTO 번호가 총 사진 개수보다 커지면 안 된다
- 모든 PHOTO 토큰을 정확히 한 번씩 사용한다
- 같은 PHOTO 토큰을 단독 사진과 사진묶음에서 중복 사용하지 않는다
- 첨부 순서는 촬영 순서가 아니라 단순 입력 순서일 수 있다
- 사진 내용을 직접 분석한 후 적절한 구간에 배치한다

올바른 예:
[사진:PHOTO_001 | 매장 내부]
[사진묶음:PHOTO_002, PHOTO_003 | 초밥 전체와 근접 모습]

잘못된 예:
[사진:PHOTO_030 | 존재하지 않는 사진]
[사진:실제파일명.jpg | 실제 파일명을 직접 작성]

[학습한 편집 스타일 프로필]
{style_text}

[실제 블로그 말투 예시]
아래 예시는 사실을 가져오기 위한 자료가 아니다.
장소명, 메뉴명, 가격, 주소와 방문 경험은 절대 새 글에 복사하지 않는다.
말투, 문장 호흡, 줄바꿈, 감탄 방식과 소제목 흐름만 참고한다.

{style_examples_text}

[사실 판단 우선순위]
1. 사용자가 직접 입력한 방문 정보
2. 첨부 사진에서 명확하게 확인되는 내용
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

[사진 분류]
사진을 내부적으로 다음 항목으로 분류한다.
- 매장 외관
- 매장 내부
- 메뉴판과 안내문
- 음식이 나오기 전 기본 상차림
- 주문한 음식 전체 모습
- 음식 근접 사진
- 음식을 먹는 장면
- 식사 후 빈 접시와 트레이
- GIF 또는 짧은 움직이는 이미지

[기본 상차림 판독]
- 트레이 가운데가 비었다는 이유만으로 식사 후라고 판단하지 않는다
- 샐러드, 절임 반찬, 간장, 와사비와 장국이 깨끗하게 남아 있으면 기본 상차림이다
- 빈 메인 접시, 음식물 흔적과 사용한 식기가 명확할 때만 식사 후라고 쓴다
- 불분명하면 촬영 시점을 단정하지 않고 사진에서 보이는 것만 설명한다
- 사용자가 알려준 촬영 시점이 사진 추정보다 우선한다

[사진 배치 흐름]
1. 매장 내부 또는 외관
2. 메뉴판 또는 안내문
3. 기본 상차림
4. 주문 메뉴 전체 모습
5. 메뉴별 근접 사진
6. 먹는 장면과 GIF
7. 명확한 식사 후 사진
8. 마무리
9. 지도

첨부 사진에 없는 단계는 생략한다.
같은 음식의 전체 사진과 근접 사진은 가까운 위치에 배치한다.

[사진 판독 정확성]
- 안내문 글씨를 한 글자씩 확인한다
- 글씨를 정확히 읽지 못하면 구체적인 메뉴명을 쓰지 않는다
- 사진에서 냉모밀처럼 확실한 메뉴는 모호한 표현으로 바꾸지 않는다
- 불확실한 생선 종류와 재료를 임의로 단정하지 않는다
- 와이파이 비밀번호와 개인정보를 쓰지 않는다
- 사진만 보고 혼잡도, 좌석 간격, 혼밥 적합 여부와 모임 적합성을 추측하지 않는다
- 사진만으로 알 수 없는 맛, 향과 식감을 만들지 않는다

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
) -> list[str]:
    command = [
        codex_command,
        "--search",
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--cd",
        str(PROJECT_ROOT),
    ]

    for image_path in image_paths:
        command.extend(["--image", str(image_path)])

    # 프롬프트는 긴 명령행 인자가 아니라 표준 입력으로 전달한다.
    command.append("-")
    return command


def _run_codex(
    codex_command: str,
    image_paths: Sequence[Path],
    prompt: str,
) -> str:
    command = _build_codex_command(
        codex_command=codex_command,
        image_paths=image_paths,
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
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            "Codex의 블로그 글 생성 시간이 15분을 초과했습니다."
        ) from error
    except OSError as error:
        raise RuntimeError(
            f"Codex CLI를 실행하지 못했습니다: {error}"
        ) from error

    if result.returncode != 0:
        error_message = result.stderr.strip()
        if not error_message:
            error_message = "Codex CLI에서 상세 오류를 반환하지 않았습니다."

        raise RuntimeError(
            "Codex 블로그 글 생성에 실패했습니다.\n\n"
            f"{error_message}"
        )

    blog_post = result.stdout.strip()
    if not blog_post:
        raise RuntimeError("Codex가 빈 블로그 글을 반환했습니다.")

    return blog_post


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

    original_prompt = build_blog_prompt(
        image_paths=resolved_image_paths,
        restaurant_name=restaurant_name.strip(),
        restaurant_location=restaurant_location.strip(),
        restaurant_points=cleaned_points,
        required_keywords=required_keywords,
        style_profile=style_profile,
    )

    current_prompt = original_prompt
    last_error: Exception | None = None

    for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
        raw_blog_post = _run_codex(
            codex_command=codex_command,
            image_paths=resolved_image_paths,
            prompt=current_prompt,
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
