import re
import pyperclip
from pathlib import Path
from playwright.sync_api import (
    Frame,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from collections.abc import Sequence
from .naver_content_writer import write_naver_content
from naver_blog_ai.paths import PROJECT_ROOT

# PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE_DIR = PROJECT_ROOT / "img"

NAVER_BLOG_ID = "dmswjddid37"
NAVER_BLOG_URL = f"https://blog.naver.com/{NAVER_BLOG_ID}"
NAVER_WRITE_URL = (
    f"https://blog.naver.com/{NAVER_BLOG_ID}"
    "/postwrite?categoryNo=43"
)

CHROME_CDP_URL = "http://127.0.0.1:9222"

PAGE_TIMEOUT_MS = 20_000

def input_naver_tags(
    page: Page,
    editor_frame: Frame,
    tags: Sequence[str],
) -> None:
    """
    발행 설정 창을 열고 네이버 태그 입력란에
    태그를 하나씩 입력한다.

    최종 발행 버튼은 누르지 않는다.
    """

    normalized_tags: list[str] = []

    for tag in tags:
        normalized_tag = (
            tag
            .replace("\ufeff", "")
            .replace("\u200b", "")
            .replace("#", "")
            .strip()
        )

        # 네이버는 Space로 태그를 확정하므로
        # 태그 내부 공백을 제거함
        normalized_tag = re.sub(
            r"\s+",
            "",
            normalized_tag,
        )

        if (
            normalized_tag
            and normalized_tag not in normalized_tags
        ):
            normalized_tags.append(normalized_tag)

    if not normalized_tags:
        print("입력할 네이버 태그가 없습니다.")
        return

    # 네이버 태그 입력란 기준 최대 30개
    normalized_tags = normalized_tags[:30]

    publish_button_selectors = [
        "button[data-click-area='tpb.publish']",
        "button.publish_btn__m9KHH",
        "button:has(span:text-is('발행'))",
    ]

    publish_button = find_visible_locator(
        editor_frame,
        publish_button_selectors,
    )

    if publish_button is None:
        publish_button = find_visible_locator(
            page,
            publish_button_selectors,
        )

    if publish_button is None:
        raise RuntimeError(
            "네이버 블로그의 발행 설정 버튼을 찾지 못했습니다."
        )

    publish_button.scroll_into_view_if_needed()
    publish_button.click()

    # 발행 설정 패널이 열리는 시간
    page.wait_for_timeout(1_000)

    tag_input_selectors = [
        "#tag-input",
        "input.tag_input__rvUB5",
        "input[data-click-area='tpb*i.tag']",
        "input[placeholder*='태그 입력']",
    ]

    tag_input = find_visible_locator(
        editor_frame,
        tag_input_selectors,
    )

    if tag_input is None:
        tag_input = find_visible_locator(
            page,
            tag_input_selectors,
        )

    if tag_input is None:
        raise RuntimeError(
            "발행 설정 창은 열렸지만 "
            "네이버 태그 입력란을 찾지 못했습니다."
        )

    tag_input.scroll_into_view_if_needed()
    tag_input.click()

    for index, tag in enumerate(
        normalized_tags,
        start=1,
    ):
        # 이전 입력값이 남아 있으면 제거
        try:
            tag_input.fill("")
        except Exception:
            tag_input.click()
            page.keyboard.press("ControlOrMeta+A")
            page.keyboard.press("Backspace")

        # 한글 태그는 키보드 타이핑보다 fill이 안정적임
        tag_input.fill(tag)

        page.wait_for_timeout(150)

        # 사용자가 확인한 네이버 태그 확정 방식
        page.keyboard.press("Space")

        page.wait_for_timeout(250)

        # Space 처리 후 입력값이 비워졌는지 확인
        try:
            remaining_value = tag_input.input_value().strip()
        except Exception:
            remaining_value = ""

        if remaining_value:
            # Space로 확정되지 않았을 때 한 번 더 시도
            page.keyboard.press("Space")
            page.wait_for_timeout(250)

        print(
            f"태그 입력 완료 "
            f"({index}/{len(normalized_tags)}): {tag}"
        )

    print(
        f"\n총 {len(normalized_tags)}개의 "
        "네이버 태그를 입력했습니다."
    )


def parse_blog_post(
    blog_post: str,
) -> tuple[str, str, list[str]]:
    """생성된 글에서 제목, 본문과 네이버 태그를 분리한다."""

    normalized_post = (
        blog_post
        .replace("\ufeff", "")
        .replace("\u200b", "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )

    post_pattern = re.compile(
        r"^\s*\*{0,2}# 제목\*{0,2}\s*\n+"
        r"(?P<title>.*?)"
        r"\n+\s*\*{0,2}# 본문\*{0,2}\s*\n+"
        r"(?P<body>.*)$",
        re.DOTALL,
    )

    post_match = post_pattern.match(normalized_post)

    if post_match is None:
        raise ValueError(
            "블로그 글에서 '# 제목'과 '# 본문'을 찾지 못했습니다."
        )

    title = post_match.group("title").strip()
    body_with_tags = post_match.group("body").strip()

    if not title:
        raise ValueError("블로그 제목이 비어 있습니다.")

    if not body_with_tags:
        raise ValueError("블로그 본문이 비어 있습니다.")

    tag_pattern = re.compile(
        r"\n*\[태그]\s*\n"
        r"(?P<tags>.*?)"
        r"\n\s*\[/태그]\s*$",
        re.DOTALL,
    )

    tag_match = tag_pattern.search(body_with_tags)

    if tag_match is None:
        raise ValueError(
            "블로그 글에서 [태그]와 [/태그]를 찾지 못했습니다."
        )

    tags = [
        line.strip().lstrip("#").strip()
        for line in tag_match.group("tags").splitlines()
        if line.strip().lstrip("#").strip()
    ]

    # 태그 블록은 네이버 본문에 입력하지 않음
    body = body_with_tags[:tag_match.start()].strip()

    if not body:
        raise ValueError("태그를 제외한 블로그 본문이 비어 있습니다.")

    if not tags:
        raise ValueError("생성된 네이버 태그가 없습니다.")

    # 순서는 유지하면서 중복 제거
    unique_tags = list(dict.fromkeys(tags))

    return title, body, unique_tags


def find_visible_locator(
    context: Page | Frame,
    selectors: list[str],
) -> Locator | None:
    """페이지 또는 프레임에서 화면에 표시된 요소를 찾는다."""

    for selector in selectors:
        locator = context.locator(selector).first

        try:
            if (
                locator.count() > 0
                and locator.is_visible()
            ):
                return locator
        except PlaywrightTimeoutError:
            continue

    return None

def open_blog_write_page(
    page: Page,
) -> Frame:
    """
    블로그에 접속하고 mainFrame 안의 글쓰기 버튼을 클릭한다.

    클릭 후 스마트에디터가 열린 mainFrame을 반환한다.
    """

    page.goto(
        NAVER_BLOG_URL,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT_MS,
    )

    page.wait_for_timeout(1_500)

    main_frame = page.frame(
        name="mainFrame",
    )

    if main_frame is None:
        raise RuntimeError(
            "네이버 블로그의 mainFrame을 찾지 못했습니다."
        )

    # 로그인 상태 확인
    login_link = main_frame.get_by_text(
        "로그인",
        exact=True,
    )

    if (
        login_link.count() > 0
        and login_link.first.is_visible()
    ):
        raise RuntimeError(
            "네이버에 로그인되어 있지 않습니다.\n"
            "start_naver_chrome.bat으로 실행한 Chrome에서 "
            "먼저 로그인해주세요."
        )

    # 사용자가 확인한 실제 글쓰기 링크를 정확히 선택
    write_button = main_frame.locator(
        f'a[href="{NAVER_WRITE_URL}"]'
        '[target="mainFrame"]'
        '.col._checkBlock._rosRestrict'
    ).first

    if write_button.count() == 0:
        # 클래스가 변경된 경우를 위한 보조 선택자
        write_button = main_frame.locator(
            'a[href*="/postwrite?categoryNo=43"]'
            '[target="mainFrame"]'
        ).first

    if write_button.count() == 0:
        raise RuntimeError(
            "mainFrame에서 글쓰기 버튼을 찾지 못했습니다."
        )

    if not write_button.is_visible():
        raise RuntimeError(
            "글쓰기 버튼을 찾았지만 화면에 표시되지 않습니다."
        )

    # 이 링크는 target="mainFrame"이므로
    # 최상위 page가 아니라 mainFrame 내부가 이동함
    write_button.click()

    try:
        main_frame.wait_for_url(
            re.compile(
                r".*/postwrite\?categoryNo=43(?:&.*)?$"
            ),
            timeout=PAGE_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "글쓰기 버튼을 눌렀지만 "
            "글쓰기 페이지로 이동하지 못했습니다."
        ) from error

    page.wait_for_timeout(2_000)

    # 페이지 이동 후 최신 프레임 객체를 다시 가져옴
    editor_frame = page.frame(
        name="mainFrame",
    )

    if editor_frame is None:
        raise RuntimeError(
            "글쓰기 화면의 mainFrame을 찾지 못했습니다."
        )

    return editor_frame

def input_title(
    page: Page,
    editor_frame: Frame,
    title: str,
) -> None:
    """네이버 스마트에디터 제목 영역에 제목을 입력한다."""

    normalized_title = (
        title
        .replace("\ufeff", "")
        .replace("\u200b", "")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )

    if not normalized_title:
        raise ValueError("입력할 블로그 제목이 비어 있습니다.")

    # 스마트에디터 제목 영역이 나타날 때까지 기다림
    title_section = editor_frame.locator(
        ".se-section-documentTitle"
    ).first

    try:
        title_section.wait_for(
            state="visible",
            timeout=20_000,
        )
    except Exception as error:
        raise RuntimeError(
            "네이버 스마트에디터의 제목 영역을 찾지 못했습니다."
        ) from error

    # 네이버 에디터 버전에 따라 실제 편집 요소가 다를 수 있음
    title_selectors = [
        ".se-section-documentTitle [contenteditable='true']",
        ".se-documentTitle [contenteditable='true']",
        ".se-section-documentTitle .se-text-paragraph",
        ".se-documentTitle .se-text-paragraph",
        ".se-title-text",
    ]

    title_editor = None

    for selector in title_selectors:
        candidates = editor_frame.locator(selector)

        for index in range(candidates.count()):
            candidate = candidates.nth(index)

            if candidate.is_visible():
                title_editor = candidate
                break

        if title_editor is not None:
            break

    if title_editor is None:
        raise RuntimeError(
            "제목 입력 요소를 찾지 못했습니다.\n"
            "네이버 스마트에디터 구조가 변경되었을 수 있습니다."
        )

    title_editor.scroll_into_view_if_needed()
    title_editor.click()
    page.wait_for_timeout(300)

    # contenteditable이면 fill() 우선 사용
    try:
        title_editor.fill(normalized_title)

    except Exception:
        # fill()이 거부되는 네이버 에디터에서는 실제 키 입력 사용
        pyperclip.copy(normalized_title)
        page.keyboard.press("Control+V")

    page.wait_for_timeout(700)
    print(f"제목 입력 완료: {normalized_title}")

def input_body(
    page: Page,
    editor_frame: Frame,
    body: str,
    image_dir: Path | None = None,
) -> None:
    """마커를 실제 네이버 에디터 요소로 변환하여 입력한다."""

    actual_image_dir = (
        image_dir.resolve()
        if image_dir is not None
        else DEFAULT_IMAGE_DIR.resolve()
    )

    write_naver_content(
        page=page,
        editor_frame=editor_frame,
        body=body,
        image_dir=actual_image_dir,
    )

def fill_naver_blog_draft(
    blog_post: str,
    image_dir: Path | None = None,
) -> None:
    """
    로그인된 Chrome에 연결해 네이버 블로그 글쓰기 화면에
    제목과 본문을 입력한다.

    발행 버튼은 누르지 않는다.
    """

    title, body, tags = parse_blog_post(blog_post)

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.connect_over_cdp(
                CHROME_CDP_URL,
                timeout=PAGE_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError as error:
            raise RuntimeError(
                "Chrome에 연결하지 못했습니다.\n"
                "start_naver_chrome.bat을 먼저 실행해주세요."
            ) from error

        if not browser.contexts:
            raise RuntimeError(
                "Chrome 브라우저 컨텍스트를 찾지 못했습니다."
            )

        context = browser.contexts[0]

        if context.pages:
            page = context.pages[0]
        else:
            page = context.new_page()

        page.set_default_timeout(PAGE_TIMEOUT_MS)

        editor_frame = open_blog_write_page(page)

        input_title(
            page=page,
            editor_frame=editor_frame,
            title=title,
        )

        input_body(
            page=page,
            editor_frame=editor_frame,
            body=body,
            image_dir=image_dir,
        )

        input_naver_tags(
            page=page,
            editor_frame=editor_frame,
            tags=tags,
        )

        print("\n네이버 블로그 입력이 완료됐습니다.")
        print("- 제목 입력 완료")
        print("- 본문 입력 완료")
        print(f"- 태그 {len(tags)}개 입력 완료")
        print("\n최종 내용을 확인한 뒤 직접 발행해주세요.")

        # CDP 연결만 해제하며 사용자가 연 Chrome은 닫지 않음
        browser.close()