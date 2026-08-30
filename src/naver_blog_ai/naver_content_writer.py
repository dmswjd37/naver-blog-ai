from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Final

import pyperclip
from playwright.sync_api import Frame, Locator, Page, TimeoutError as PlaywrightTimeoutError


INLINE_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"\[굵게:(?P<bold>.+?)\]"
    r"|\[색상:[^\]]+\](?P<color>.+?)\[/색상\]"
)

PHOTO_RE: Final[re.Pattern[str]] = re.compile(
    r"^\[사진:(?P<filename>.+?)\s*\|\s*(?P<description>.*?)\]$"
)

PHOTO_GROUP_RE: Final[re.Pattern[str]] = re.compile(
    r"^\[사진묶음:(?P<filenames>.+?)\s*\|\s*(?P<description>.*?)\]$"
)

HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^\[소제목:(?P<value>.*?)\]$"
)

MINOR_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^\[소소제목:(?P<value>.*?)\]$"
)

LEGACY_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^\[소제목\](?P<value>.*?)\[/소제목\]$"
)

LEGACY_MINOR_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^\[소소제목\](?P<value>.*?)\[/소소제목\]$"
)

SIMPLE_QUOTE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\[인용구:(?P<value>.*?)\]$"
)

SKIPPED_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"^\[(?:스티커|지도)(?::[^\]]*)?\]$"
)

SUPPORTED_IMAGE_SUFFIXES: Final[set[str]] = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
}

FILE_INPUT_SELECTORS = [
    "input[type='file'][accept*='image']",
    "input[type='file'][accept*='.jpg']",
    "input[type='file'][accept*='.jpeg']",
    "input[type='file'][accept*='.png']",
    "input[type='file'][multiple]",
    "input[type='file']",
]


def _find_photo_file_input(
    page: Page,
    editor_frame: Frame,
) -> Locator | None:
    """
    네이버가 만든 숨겨진 사진 업로드 input을 찾는다.

    input[type=file]은 화면에 보이지 않는 것이 정상이므로
    is_visible()로 판단하면 안 된다.
    """

    for context in (editor_frame, page):
        for selector in FILE_INPUT_SELECTORS:
            candidates = context.locator(selector)
            count = candidates.count()

            # 가장 최근에 만들어진 input부터 확인
            for index in range(count - 1, -1, -1):
                candidate = candidates.nth(index)

                try:
                    if candidate.is_enabled():
                        return candidate
                except Exception:
                    continue

    return None


def _find_photo_button(
    editor_frame: Frame,
) -> Locator:
    selectors = [
        "button[data-name='image']",
        "button[data-name='photo']",
        "button.se-toolbar-option-image-button",
        "button[class*='insert-image']",
        "button[class*='image-toolbar']",
    ]

    for selector in selectors:
        candidates = editor_frame.locator(selector)

        for index in range(candidates.count()):
            candidate = candidates.nth(index)

            try:
                if candidate.is_visible() and candidate.is_enabled():
                    return candidate
            except Exception:
                continue

    raise RuntimeError("네이버 에디터의 사진 버튼을 찾지 못했습니다.")

def _upload_single_photo(
    page: Page,
    editor_frame: Frame,
    image_path: Path,
) -> None:
    """사진을 정확히 한 장만 업로드한다."""

    resolved_path = image_path.resolve()

    if not resolved_path.is_file():
        raise FileNotFoundError(
            f"사진 파일을 찾을 수 없습니다: {resolved_path}"
        )

    # 이전 업로드에서 생성된 input을 재사용할 수 있으면
    # 사진 버튼을 누르지 않고 바로 파일을 지정
    file_input = _find_photo_file_input(
        page=page,
        editor_frame=editor_frame,
    )

    if file_input is not None:
        try:
            file_input.set_input_files(
                str(resolved_path),
                timeout=10_000,
            )
            page.wait_for_timeout(1_500)
            return
        except Exception:
            # 이전 input이 제거됐거나 더는 사용할 수 없는 경우
            # 아래 filechooser 방식으로 다시 시도
            pass

    photo_button = _find_photo_button(editor_frame)
    photo_button.scroll_into_view_if_needed()

    try:
        with page.expect_file_chooser(
            timeout=5_000,
        ) as chooser_info:
            photo_button.click(
                force=True,
                timeout=5_000,
            )

        chooser_info.value.set_files(
            str(resolved_path),
            timeout=10_000,
        )

    except PlaywrightTimeoutError:
        # 버튼 클릭으로 input만 동적으로 생성되고
        # filechooser 이벤트는 발생하지 않은 경우
        file_input = _find_photo_file_input(
            page=page,
            editor_frame=editor_frame,
        )

        if file_input is None:
            raise RuntimeError(
                "사진 버튼 클릭 후 filechooser 이벤트와 "
                "input[type=file]을 모두 찾지 못했습니다."
            )

        file_input.set_input_files(
            str(resolved_path),
            timeout=10_000,
        )

    # 네이버가 사진 컴포넌트를 만드는 시간을 기다림
    page.wait_for_timeout(2_000)

def normalize_editor_text(value: str) -> str:
    """BOM과 보이지 않는 공백을 제거한다."""
    return (
        value.replace("\ufeff", "")
        .replace("\u200b", "")
        .replace("\u00a0", " ")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


def _first_visible(locator: Locator) -> Locator | None:
    for index in range(locator.count()):
        candidate = locator.nth(index)

        if candidate.is_visible():
            return candidate

    return None


def _copy_and_paste(page: Page, text: str) -> None:
    if not text:
        return

    pyperclip.copy(text)
    page.keyboard.press("ControlOrMeta+V")


def _body_paragraphs(editor_frame: Frame) -> Locator:
    return editor_frame.locator(
        ".se-component.se-text:not(.se-documentTitle) .se-section-text, "
        ".se-component.se-text:not(.se-documentTitle) .se-text-paragraph, "
        ".se-component.se-text:not(.se-documentTitle) [contenteditable='true'], "
        ".se-section-text .se-text-paragraph, "
        ".se-section-text [contenteditable='true'], "
        ".se-section-text, "
        ".se-component-text:not(.se-documentTitle) .se-text-paragraph, "
        ".se-component-text:not(.se-documentTitle) [contenteditable='true'], "
        "[contenteditable='true'][data-placeholder*='본문'], "
        "[role='textbox'][contenteditable='true']"
    )


def _focus_last_body_paragraph(editor_frame: Frame) -> Locator:
    deadline = time.monotonic() + 15

    while time.monotonic() < deadline:
        paragraphs = _body_paragraphs(editor_frame)

        for index in range(paragraphs.count() - 1, -1, -1):
            paragraph = paragraphs.nth(index)

            try:
                if not paragraph.is_visible():
                    continue

                is_title_element = paragraph.evaluate(
                    "element => Boolean("
                    "element.closest("
                    "'.se-documentTitle, .se-section-documentTitle'"
                    ")"
                    ")"
                )

                if is_title_element:
                    continue

                paragraph.scroll_into_view_if_needed()
                paragraph.click()
                return paragraph
            except Exception:
                # 스마트에디터가 다시 렌더링하면서 요소가 교체된 경우 재탐색
                continue

        placeholder_selectors = [
            ".se-placeholder.__se_placeholder",
            "[data-placeholder*='본문']",
            "text=/본문을\\s*입력/",
        ]

        for selector in placeholder_selectors:
            placeholder = _first_visible(editor_frame.locator(selector))

            if placeholder is None:
                continue

            try:
                is_title_element = placeholder.evaluate(
                    "element => Boolean("
                    "element.closest("
                    "'.se-documentTitle, .se-section-documentTitle'"
                    ")"
                    ")"
                )

                if is_title_element:
                    continue

                placeholder.scroll_into_view_if_needed()
                placeholder.click()
                return placeholder
            except Exception:
                continue

        time.sleep(0.2)

    raise RuntimeError("네이버 스마트에디터 본문 입력 영역을 찾지 못했습니다.")


def _set_body_center_alignment(
    page: Page,
    editor_frame: Frame,
) -> None:
    """현재 본문 문단을 가운데 정렬한다."""

    center_selector = (
        "button"
        "[data-name='align-drop-down-with-justify']"
        "[data-role='option']"
        "[data-value='center']"
    )

    center_option = editor_frame.locator(
        center_selector
    ).first

    # 옵션이 DOM에 있지만 숨겨진 경우
    if center_option.count() > 0:
        try:
            if center_option.is_visible():
                center_option.click()
            else:
                # 숨겨진 정렬 옵션에 클릭 이벤트 직접 전달
                center_option.dispatch_event("click")

            page.wait_for_timeout(300)
            return

        except Exception:
            pass

    # 가운데 정렬 옵션이 DOM에 없으면 정렬 메뉴부터 열기
    align_menu_selectors = [
        (
            "button"
            "[data-name='align-drop-down-with-justify']"
            "[data-role='select']"
        ),
        (
            "button"
            "[data-name='align-drop-down-with-justify']"
            ":not([data-role='option'])"
        ),
        "button[class*='align'][class*='drop-down']",
        "button[class*='align'][class*='select']",
        "button:has(.se-toolbar-tooltip:text-is('정렬'))",
        "button:has(.se-toolbar-tooltip:text-is('문단 정렬'))",
    ]

    align_menu: Locator | None = None

    for selector in align_menu_selectors:
        candidates = editor_frame.locator(selector)

        for index in range(candidates.count()):
            candidate = candidates.nth(index)

            try:
                if candidate.is_visible():
                    align_menu = candidate
                    break
            except Exception:
                continue

        if align_menu is not None:
            break

    if align_menu is not None:
        align_menu.click()
        page.wait_for_timeout(300)

        center_option = editor_frame.locator(
            center_selector
        ).first

        try:
            center_option.wait_for(
                state="visible",
                timeout=3_000,
            )
            center_option.click()
            page.wait_for_timeout(300)
            return

        except Exception:
            pass

    # 에디터 구조가 다른 경우 키보드 단축키로 처리
    print(
        "가운데 정렬 버튼을 직접 누르지 못해 "
        "키보드 단축키로 가운데 정렬합니다."
    )

    page.keyboard.press("ControlOrMeta+Shift+E")
    page.wait_for_timeout(300)

def _write_inline_text(
    page: Page,
    editor_frame: Frame,
    text: str,
) -> None:
    """
    문장 전체를 일반 글씨로 입력한 뒤,
    키보드 선택으로 [굵게:내용]만 굵게 처리한다.

    editor_frame은 호출 형식을 유지하기 위해 받지만
    현재 함수에서는 직접 사용하지 않는다.
    """

    plain_parts: list[str] = []
    bold_ranges: list[tuple[int, int]] = []

    source_position = 0
    output_position = 0

    for match in INLINE_MARKER_RE.finditer(text):
        # 마커 앞에 있는 일반 글자
        before_text = text[
            source_position:match.start()
        ]

        plain_parts.append(before_text)
        output_position += len(before_text)

        bold_text = match.group("bold")
        color_text = match.group("color")

        if bold_text is not None:
            bold_start = output_position

            plain_parts.append(bold_text)
            output_position += len(bold_text)

            bold_end = output_position

            bold_ranges.append(
                (bold_start, bold_end)
            )

        elif color_text is not None:
            # 색상 마커는 제거하고 글자만 입력
            plain_parts.append(color_text)
            output_position += len(color_text)

        source_position = match.end()

    # 마지막 마커 뒤의 일반 글자
    remaining_text = text[source_position:]
    plain_parts.append(remaining_text)
    output_position += len(remaining_text)

    plain_text = "".join(plain_parts)

    # 문장 전체를 일반 글씨로 한 번만 입력
    pyperclip.copy(plain_text)
    page.keyboard.press("ControlOrMeta+V")
    page.wait_for_timeout(500)

    if not bold_ranges:
        return

    # 굵은 영역 중 하나가 문장 끝까지 이어지는지 확인
    ends_with_bold = any(
        bold_end == len(plain_text)
        for _, bold_end in bold_ranges
    )

    # 현재 커서는 문장의 마지막에 있음
    current_position = len(plain_text)

    # 뒤쪽 굵게 마커부터 처리
    for bold_start, bold_end in reversed(bold_ranges):
        # 현재 위치에서 굵게 대상의 끝까지 이동
        move_left_count = current_position - bold_end

        for _ in range(move_left_count):
            page.keyboard.press("ArrowLeft")

        # 굵게 대상만 왼쪽 방향으로 선택
        select_count = bold_end - bold_start

        for _ in range(select_count):
            page.keyboard.press("Shift+ArrowLeft")

        page.wait_for_timeout(100)

        # 선택된 글자에만 굵게 적용
        page.keyboard.press("ControlOrMeta+B")
        page.wait_for_timeout(250)

        # 선택을 풀고 커서를 굵은 글자의 시작 위치에 둔다
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(100)        

        current_position = bold_start

    # 모든 굵게 처리가 끝나면 다시 문장 끝으로 이동
    move_right_count = len(plain_text) - current_position

    for _ in range(move_right_count):
        page.keyboard.press("ArrowRight")

    page.wait_for_timeout(200)

    # 문장이 굵은 글자로 끝났는지 호출한 곳에 알려줌
    return ends_with_bold


def _write_text_line(
    page: Page,
    editor_frame: Frame,
    text: str,
) -> None:
    ends_with_bold = _write_inline_text(
        page=page,
        editor_frame=editor_frame,
        text=text,
    )
    # 클립보드 붙여넣기가 에디터 상태에 반영되기 전에 Enter가 먼저
    # 처리되면 다음 문장이 같은 줄에 붙을 수 있어 짧게 기다린다.
    page.wait_for_timeout(200)
    page.keyboard.press("Enter")
    page.wait_for_timeout(150)

    # 새 줄이 이전 줄의 굵은 상태를 물려받았다면 해제
    if ends_with_bold:
        page.keyboard.press("ControlOrMeta+B")
        page.wait_for_timeout(150)


# 인용구 선택
def _click_quotation_style(
    editor_frame: Frame,
    data_value: str,
) -> None:
    option_selector = (
        "button[data-name='quotation']"
        f"[data-value='{data_value}']"
    )
    option_locator = editor_frame.locator(option_selector).first

    # 일부 에디터 버전은 인용구 선택 메뉴를 열기 전까지
    # 세부 옵션 버튼을 DOM에 생성하지 않는다.
    if option_locator.count() == 0:
        menu_button_selectors = [
            "button[data-name='quotation'][data-role='select']",
            "button[data-name='quotation'][aria-haspopup='true']",
            "button[class*='quotation'][class*='select-button']",
            "button[class*='quotation'][class*='arrow-button']",
            "button[class*='quotation'][class*='more-button']",
            "button[class*='quotation'][class*='expand-button']",
        ]

        menu_button: Locator | None = None

        for selector in menu_button_selectors:
            menu_button = _first_visible(editor_frame.locator(selector))

            if menu_button is not None:
                break

        if menu_button is None:
            quotation_buttons = editor_frame.locator(
                "button[data-name='quotation'], "
                "button[class*='quotation']"
            )
            button_details = quotation_buttons.evaluate_all(
                "buttons => buttons.map(button => ({"
                "className: button.className, "
                "dataName: button.getAttribute('data-name'), "
                "dataRole: button.getAttribute('data-role'), "
                "ariaHaspopup: button.getAttribute('aria-haspopup'), "
                "ariaLabel: button.getAttribute('aria-label'), "
                "text: button.innerText"
                "}))"
            )

            raise RuntimeError(
                "네이버 인용구 선택 메뉴 버튼을 찾지 못했습니다.\n"
                f"발견한 인용구 버튼 정보: {button_details}"
            )

        menu_button.click()

    try:
        option_locator.wait_for(state="attached", timeout=5_000)
    except Exception as error:
        raise RuntimeError(
            "인용구 선택 메뉴를 열었지만 옵션을 찾지 못했습니다: "
            f"{data_value}"
        ) from error

    if option_locator.is_visible():
        option_locator.click()
    else:
        # 기본 인용구 삽입 버튼을 누르지 않고 숨겨진 세부 옵션에
        # 직접 click 이벤트를 전달한다.
        option_locator.dispatch_event("click")


# 구분선 선택
def _click_horizontal_line_style(
    editor_frame: Frame,
    data_value: str,
) -> None:
    """구분선 선택 목록을 열고 지정한 구분선을 선택한다."""

    page = editor_frame.page

    # 1. 구분선 목록 버튼
    menu_selector = (
        "button"
        "[data-group='documentToolbar']"
        "[data-type='icon-select']"
        "[data-name='horizontal-line']"
    )

    try:
        editor_frame.locator(menu_selector).nth(1).click(
            timeout=5_000,
        )
    except Exception as error:
        raise RuntimeError(
            "두 번째 메뉴 버튼을 클릭하지 못했습니다."
        ) from error
    
    # 3. 목록이 열린 후 생성되는 line5 버튼
    option_selector = (
        "button."
        "se-toolbar-option-insert-horizontal-line-"
        f"{data_value}-button"
    )

    frames = [
        editor_frame,
        *[
            frame
            for frame in page.frames
            if frame != editor_frame
        ],
    ]

    def find_visible_option() -> Locator | None:
        """현재 표시된 구분선 옵션을 찾는다."""

        for frame in frames:
            candidates = frame.locator(option_selector)

            for index in range(candidates.count()):
                candidate = candidates.nth(index)

                try:
                    if (
                        candidate.is_visible()
                        and candidate.is_enabled()
                    ):
                        return candidate
                except Exception:
                    continue

        return None

    # 옵션이 나타날 때까지 최대 5초 대기
    option_button: Locator | None = None

    for _ in range(50):
        option_button = find_visible_option()

        if option_button is not None:
            break

        page.wait_for_timeout(100)

    # 일반 click으로 메뉴가 열리지 않은 경우 한 번만 이벤트 방식 재시도
    if option_button is None:
        option_button.dispatch_event("click")
        page.wait_for_timeout(500)

        for _ in range(30):
            option_button = find_visible_option()

            if option_button is not None:
                break

            page.wait_for_timeout(100)

    if option_button is None:
        discovered_classes: list[str] = []

        for frame in frames:
            all_options = frame.locator(
                "[class*='horizontal-line']"
            )

            for index in range(all_options.count()):
                class_name = all_options.nth(index).get_attribute(
                    "class"
                )

                if (
                    class_name
                    and class_name not in discovered_classes
                ):
                    discovered_classes.append(class_name)

        raise RuntimeError(
            "구분선 선택 버튼을 클릭했지만 "
            "요청한 옵션이 나타나지 않았습니다.\n"
            f"요청한 구분선: {data_value}\n"
            f"옵션 선택자: {option_selector}\n"
            "현재 확인된 관련 클래스:\n"
            + "\n".join(
                f"- {class_name}"
                for class_name in discovered_classes
            )
        )

    # 4. line5 선택
    option_button.click()
    page.wait_for_timeout(500)


# 폰트 선택
def _click_font_size(
    editor_frame: Frame,
    data_value: str,
) -> None:
    """글자 크기 메뉴를 열고 지정한 크기를 선택한다."""

    # 1. 상단 도구 모음의 글자 크기 드롭다운 버튼
    dropdown_candidates = editor_frame.locator(
        "button[data-name='font-size']"
        ":not([data-role='option'])"
    )

    dropdown_button: Locator | None = None

    for index in range(dropdown_candidates.count()):
        candidate = dropdown_candidates.nth(index)

        try:
            if (
                candidate.is_visible()
                and candidate.is_enabled()
            ):
                dropdown_button = candidate
                break
        except Exception:
            continue

    if dropdown_button is None:
        raise RuntimeError(
            "네이버 에디터의 글자 크기 버튼을 찾지 못했습니다."
        )

    # 드롭다운 열기
    dropdown_button.click()
    editor_frame.page.wait_for_timeout(300)

    # 2. 펼쳐진 목록의 실제 옵션만 선택
    option_candidates = editor_frame.locator(
        "button[data-name='font-size']"
        "[data-role='option']"
        f"[data-value='{data_value}']"
    )

    option_locator: Locator | None = None

    for index in range(option_candidates.count()):
        candidate = option_candidates.nth(index)

        try:
            if (
                candidate.is_visible()
                and candidate.is_enabled()
            ):
                option_locator = candidate
                break
        except Exception:
            continue

    if option_locator is None:
        raise RuntimeError(
            "글자 크기 메뉴는 열었지만 "
            f"표시된 옵션을 찾지 못했습니다: {data_value}"
        )

    # 실제로 화면에 표시된 옵션을 정상 클릭
    option_locator.click()
    editor_frame.page.wait_for_timeout(300)

def _quotation_components(editor_frame: Frame) -> Locator:
    return editor_frame.locator(
        ".se-component.se-quotation, "
        ".se-component-quotation"
    )


def _wait_for_new_quotation(
    page: Page,
    editor_frame: Frame,
    previous_count: int,
) -> Locator:
    quotations = _quotation_components(editor_frame)
    deadline = time.monotonic() + 10

    while time.monotonic() < deadline:
        if quotations.count() > previous_count:
            component = quotations.nth(quotations.count() - 1)
            component.wait_for(state="visible", timeout=5_000)
            return component

        page.wait_for_timeout(200)

    raise RuntimeError("인용구 블록이 에디터에 추가되지 않았습니다.")


def _quotation_paragraphs(component: Locator) -> Locator:
    paragraphs = component.locator(".se-text-paragraph")

    if paragraphs.count():
        return paragraphs

    return component.locator("[contenteditable='true']")


# 정보박스 내부 내용 입력
def _insert_information_box(
    page: Page,
    editor_frame: Frame,
    contents: list[str],
) -> None:
    quotations = _quotation_components(editor_frame)
    previous_count = quotations.count()

    _click_quotation_style(
        editor_frame=editor_frame,
        data_value="quotation_corner",
    )

    component = _wait_for_new_quotation(
        page=page,
        editor_frame=editor_frame,
        previous_count=previous_count,
    )

    paragraphs = _quotation_paragraphs(component)

    if not paragraphs.count():
        raise RuntimeError("정보박스 내부 입력 영역을 찾지 못했습니다.")

    paragraphs.first.click()
    page.wait_for_timeout(200)

    # 정보박스 글자의 크기를 16으로 설정
    _click_font_size(
        editor_frame=editor_frame,
        data_value="fs16",
    )

    _copy_and_paste(
        page,
        "\n".join(contents).strip(),
    )
    page.wait_for_timeout(300)

    # 인용구의 마지막 입력 영역으로 이동
    paragraphs.last.click()
    page.keyboard.press("End")

    # 아래 방향키 두 번
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(150)
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(300)

def _insert_heading_divider(
    page: Page,
    editor_frame: Frame,
    heading: str,
    minor_heading: str | None,
) -> None:
    quotations = _quotation_components(editor_frame)
    previous_count = quotations.count()

    _click_quotation_style(
        editor_frame=editor_frame,
        data_value="quotation_underline",
    )

    component = _wait_for_new_quotation(
        page=page,
        editor_frame=editor_frame,
        previous_count=previous_count,
    )

    paragraphs = _quotation_paragraphs(component)

    if not paragraphs.count():
        raise RuntimeError("구분선 인용구 내부 입력 영역을 찾지 못했습니다.")

    # 첫 번째 입력칸: 소제목
    paragraphs.nth(0).click()
    _copy_and_paste(page, heading)
    page.wait_for_timeout(200)

    if minor_heading:
        # 두 번째 입력칸: 소소제목
        if paragraphs.count() < 2:
            raise RuntimeError(
                "구분선 인용구의 두 번째 입력 영역을 찾지 못했습니다."
            )

        paragraphs.nth(1).click()
        _copy_and_paste(page, minor_heading)
        page.wait_for_timeout(200)

        # 소소제목이 있으면 아래 방향키 한 번 + Enter
        paragraphs.nth(1).click()
        page.keyboard.press("End")
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(150)
        page.keyboard.press("Enter")

    else:
        # 소제목만 있으면 아래 방향키 두 번 + Enter
        paragraphs.nth(0).click()
        page.keyboard.press("End")

        for _ in range(2):
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(150)

        page.keyboard.press("Enter")

    page.wait_for_timeout(300)


def _resolve_image_paths(
    image_dir: Path,
    filenames: list[str],
) -> list[Path]:
    image_root = image_dir.resolve()
    resolved_paths: list[Path] = []

    if not image_root.is_dir():
        raise NotADirectoryError(
            f"이미지 폴더를 찾을 수 없습니다: {image_root}"
        )

    for filename in filenames:
        clean_name = normalize_editor_text(filename).strip()
        candidate = (image_root / clean_name).resolve()

        try:
            candidate.relative_to(image_root)
        except ValueError as error:
            raise ValueError(
                f"이미지 폴더 밖의 파일은 사용할 수 없습니다: {clean_name}"
            ) from error

        if not candidate.is_file():
            raise FileNotFoundError(
                f"img 폴더에서 사진을 찾을 수 없습니다: {candidate}"
            )

        if candidate.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
            raise ValueError(
                f"지원하지 않는 이미지 형식입니다: {candidate.name}"
            )

        resolved_paths.append(candidate)

    return resolved_paths


def _find_photo_button(
    editor_frame: Frame,
) -> Locator:
    """상단 문서 툴바의 사진 추가 버튼만 찾는다."""

    selectors = [
        (
            "button"
            "[data-group='documentToolbar']"
            "[data-name='image']"
        ),
        (
            "button"
            "[data-group='documentToolbar']"
            "[data-name='photo']"
        ),
        (
            "button.se-toolbar-option-image-button"
            "[data-group='documentToolbar']"
        ),
        (
            "[data-group='documentToolbar'] "
            "button:has(.se-toolbar-tooltip:text-is('사진'))"
        ),
    ]

    for selector in selectors:
        candidates = editor_frame.locator(selector)

        for index in range(candidates.count()):
            candidate = candidates.nth(index)

            try:
                if (
                    candidate.is_visible()
                    and candidate.is_enabled()
                ):
                    return candidate
            except Exception:
                continue

    raise RuntimeError(
        "상단 문서 툴바의 사진 추가 버튼을 찾지 못했습니다."
    )


def _photo_components(editor_frame: Frame) -> Locator:
    return editor_frame.locator(
        ".se-component.se-image, "
        ".se-component-image"
    )


def _insert_photos(
    page: Page,
    editor_frame: Frame,
    image_dir: Path,
    filenames: list[str],
) -> None:
    """
    사진묶음 마커라도 파일 선택은 한 장씩 수행한다.

    여러 파일을 한 번에 set_input_files()에 전달하면
    '사진 첨부 방식' 팝업이 표시될 수 있다.
    """

    if not filenames:
        return

    for index, filename in enumerate(filenames):
        image_path = image_dir / filename

        print(
            f"사진 업로드 중 "
            f"({index + 1}/{len(filenames)}): {filename}"
        )

        # 매 반복마다 새로운 input과 버튼 Locator를 찾음
        _upload_single_photo(
            page=page,
            editor_frame=editor_frame,
            image_path=image_path,
        )

        print(f"사진 업로드 완료: {filename}")

        # 다음 사진을 바로 업로드할 때 네이버가
        # 현재 사진 처리를 마칠 시간을 조금 준다
        if index < len(filenames) - 1:
            if image_path.suffix.lower() == ".gif":
                print("GIF 처리 대기: 10초")
                page.wait_for_timeout(10_000)
            else:
                page.wait_for_timeout(3_000)


def write_naver_content(
    page: Page,
    editor_frame: Frame,
    body: str,
    image_dir: Path,
) -> None:
    """블로그 마커를 네이버 스마트에디터 요소로 변환해 입력한다."""
    normalized_body = normalize_editor_text(body)
    lines = normalized_body.split("\n")

    pending_heading: str | None = None
    pending_minor_heading: str | None = None
    index = 0

    # 최초 한 번만 본문을 찾는다. 이후에는 현재 커서를 유지하고,
    # 사진/인용구를 넣은 경우에만 그 컴포넌트 바로 아래로 이동한다.
    _focus_last_body_paragraph(editor_frame)

    # 첫 문장을 입력하기 전에 가운데 정렬
    _set_body_center_alignment(
        page=page,
        editor_frame=editor_frame,
    )

    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.strip()

        if line == "[마무리]":
            next_index = index + 1

            _click_horizontal_line_style(
                editor_frame=editor_frame,
                data_value="line5",
            )
            page.keyboard.press("Enter")

            index = next_index + 1
            continue

        if line == "[정보박스]":
            information_lines: list[str] = []
            index += 1

            while index < len(lines):
                information_line = lines[index]

                if information_line.strip() == "[/정보박스]":
                    break

                information_lines.append(information_line.rstrip())
                index += 1

            else:
                raise ValueError("[/정보박스] 마커가 없습니다.")

            _insert_information_box(
                page=page,
                editor_frame=editor_frame,
                contents=information_lines,
            )
            index += 1
            continue

        heading_match = HEADING_RE.fullmatch(line) or LEGACY_HEADING_RE.fullmatch(
            line
        )

        if heading_match:
            pending_heading = heading_match.group("value").strip()
            index += 1
            continue

        minor_heading_match = (
            MINOR_HEADING_RE.fullmatch(line)
            or LEGACY_MINOR_HEADING_RE.fullmatch(line)
        )

        if minor_heading_match:
            pending_minor_heading = minor_heading_match.group("value").strip()
            index += 1
            continue

        if line == "[구분선]":
            if not pending_heading:
                raise ValueError(
                    "[구분선] 앞에는 [소제목:내용]이 있어야 합니다."
                )

            _insert_heading_divider(
                page=page,
                editor_frame=editor_frame,
                heading=pending_heading,
                minor_heading=pending_minor_heading,
            )
            pending_heading = None
            pending_minor_heading = None
            index += 1
            continue

        photo_match = PHOTO_RE.fullmatch(line)

        if photo_match:
            _insert_photos(
                page=page,
                editor_frame=editor_frame,
                image_dir=image_dir,
                filenames=[photo_match.group("filename").strip()],
            )
            index += 1
            continue

        photo_group_match = PHOTO_GROUP_RE.fullmatch(line)

        if photo_group_match:
            filenames = [
                filename.strip()
                for filename in photo_group_match.group("filenames").split(",")
                if filename.strip()
            ]

            if not filenames:
                raise ValueError("[사진묶음]에 파일명이 없습니다.")

            _insert_photos(
                page=page,
                editor_frame=editor_frame,
                image_dir=image_dir,
                filenames=filenames,
            )
            index += 1
            continue

        simple_quote_match = SIMPLE_QUOTE_RE.fullmatch(line)

        if simple_quote_match:
            _write_text_line(
                page=page,
                editor_frame=editor_frame,
                text=simple_quote_match.group("value").strip(),
            )
            index += 1
            continue

        if SKIPPED_MARKER_RE.fullmatch(line):
            print(f"현재 자동 입력에서 건너뛴 마커: {line}")
            index += 1
            continue

        if not line:
            page.keyboard.press("Enter")
            index += 1
            continue

        _write_text_line(
            page=page,
            editor_frame=editor_frame,
            text=raw_line.strip(),
        )
        index += 1

    if pending_heading or pending_minor_heading:
        raise ValueError(
            "소제목 뒤에 [구분선]이 없어 입력하지 못한 제목이 있습니다."
        )