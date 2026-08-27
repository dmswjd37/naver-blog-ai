import argparse
import json
from pathlib import Path
from collections.abc import Sequence

from .image_loader import load_image_paths
from .blog_generator import generate_blog_post
from .naver_writer import fill_naver_blog_draft
from .models import RequiredKeyword
from naver_blog_ai.paths import PROJECT_ROOT

# PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_IMAGE_FOLDER = PROJECT_ROOT / "img"
STYLE_PROFILE_PATH = PROJECT_ROOT / "config" / "style_profile.json"

OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_FILE = OUTPUT_DIR / "blog_post.md"

DEFAULT_IMAGE_DIR = PROJECT_ROOT / "img"

def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="naver-blog-ai",
        description="AI 기반 네이버 맛집 블로그 생성기",
    )

    # 이미지 폴더를 프로그램의 기본 인자로 직접 등록
    parser.add_argument(
        "folder",
        nargs="?",
        type=Path,
        default=Path("img"),
        help="이미지 폴더 경로 (기본값: 프로젝트의 img 폴더)",
    )

    parser.add_argument(
        "--write-naver",
        action="store_true",
        help="생성된 글을 네이버 블로그 편집기에 입력합니다.",
    )

    parser.add_argument(
        "--write-only",
        nargs="?",
        type=Path,
        const=OUTPUT_FILE,
        default=None,
        metavar="BLOG_POST",
        help=(
            "기존 블로그 글을 네이버 편집기에 입력합니다. "
            "파일을 생략하면 output/blog_post.md를 사용합니다."
        ),
    )

    return parser

def input_restaurant_info() -> tuple[str, str]:
    """웹 검색에 사용할 가게명과 지역을 입력받는다."""
    print("\n검색할 음식점 정보를 입력해주세요.")

    while True:
        restaurant_name = input(
            "가게명과 지점명: "
        ).strip()

        if restaurant_name:
            break

        print("가게명을 입력해주세요.")

    restaurant_location = input(
        "지역 또는 주소: "
    ).strip()

    return restaurant_name, restaurant_location

def input_restaurant_points() -> list[str]:
    """블로그 글에 반영할 맛집 포인트를 입력받는다."""
    print("\n맛집 포인트를 한 줄씩 입력해주세요.")
    print("예: 유니짜장이 유명함 / 카페 같은 인테리어 / 주차 가능")
    print("입력을 끝내려면 빈 상태에서 Enter를 누르세요.\n")

    points: list[str] = []

    while True:
        point = input(
            f"포인트 {len(points) + 1}: "
        ).strip()

        if not point:
            if points:
                break

            print("맛집 포인트를 하나 이상 입력해주세요.")
            continue

        points.append(point)

    return points

def input_required_keywords() -> list[RequiredKeyword]:
    """체험단에서 요구한 필수 키워드와 사용 조건을 입력받는다."""

    print("\n체험단 필수 키워드를 입력해주세요.")
    print("예: 의정부 초밥 맛집 / 민락동 맛집")
    print("필수 키워드가 없으면 바로 Enter를 누르세요.\n")

    requirements: list[RequiredKeyword] = []

    while True:
        keyword = input(
            f"필수 키워드 {len(requirements) + 1}: "
        ).strip()

        if not keyword:
            break

        while True:
            title_answer = input(
                "제목에도 반드시 넣을까요? (y/n): "
            ).strip().lower()

            if title_answer in {"y", "yes"}:
                title_required = True
                break

            if title_answer in {"n", "no"}:
                title_required = False
                break

            print("y 또는 n으로 입력해주세요.")

        while True:
            body_count_input = input(
                "본문 최소 사용 횟수 (기본값 3회): "
            ).strip()

            if not body_count_input:
                body_min_count = 3
                break

            try:
                body_min_count = int(body_count_input)

                if body_min_count < 0:
                    raise ValueError

                break

            except ValueError:
                print("0 이상의 숫자를 입력해주세요.")

        requirements.append(
            {
                "keyword": keyword,
                "title_required": title_required,
                "body_min_count": body_min_count,
            }
        )

        print()

    return requirements


def load_style_profile() -> dict:
    """config/style_profile.json에서 말투 설정을 읽는다."""
    if not STYLE_PROFILE_PATH.exists():
        raise FileNotFoundError(
            "말투 설정 파일이 없습니다.\n"
            f"확인할 경로: {STYLE_PROFILE_PATH}"
        )

    try:
        with STYLE_PROFILE_PATH.open(
            mode="r",
            encoding="utf-8",
        ) as file:
            return json.load(file)

    except json.JSONDecodeError as error:
        raise ValueError(
            "style_profile.json 형식이 올바르지 않습니다.\n"
            f"오류 위치: {error.lineno}번째 줄 "
            f"{error.colno}번째 문자"
        ) from error


def write_existing_blog_post(
    blog_post_path: Path,
) -> int:
    """기존 글을 읽어 네이버 편집기에 입력한다."""

    if not blog_post_path.is_absolute():
        blog_post_path = PROJECT_ROOT / blog_post_path

    blog_post_path = blog_post_path.resolve()

    if not blog_post_path.exists():
        raise FileNotFoundError(
            f"블로그 글 파일을 찾을 수 없습니다: {blog_post_path}"
        )

    if not blog_post_path.is_file():
        raise ValueError(
            f"블로그 글 경로가 파일이 아닙니다: {blog_post_path}"
        )

    blog_post = blog_post_path.read_text(
        encoding="utf-8-sig",
    ).strip()

    if not blog_post:
        raise ValueError(
            f"블로그 글 파일이 비어 있습니다: {blog_post_path}"
        )

    print(f"\n블로그 글을 불러왔습니다: {blog_post_path}")
    print("네이버 블로그 편집기를 여는 중입니다.")

    fill_naver_blog_draft(
        blog_post=blog_post,
        image_dir=DEFAULT_IMAGE_DIR,
    )

    print("\n제목과 본문 입력이 완료되었습니다.")
    print("내용을 확인한 후 직접 발행해주세요.")

    return 0

# argv에는 분석할 명령어 목록을 전달할 수 있음
# 문자열 목록 또는 None을 받을 수 있다는 뜻
# 반환 타입 int: 정수는 프로그램 종료 상태 코드로 사용 (0: 정상 종료 / 1이상: 오류 종료)
def main(argv: Sequence[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)

    # 기존 글을 네이버에 입력만 하는 경우
    # 이미지 검색과 Codex 글 생성을 실행하지 않고 바로 종료
    if args.write_only is not None:
        try:
            return write_existing_blog_post(
                args.write_only,
            )
        except (
            FileNotFoundError,
            ValueError,
            RuntimeError,
        ) as error:
            print(f"\n오류: {error}")
            return 1

    try:
        # 이미지 불러오기
        image_paths = load_image_paths(args.folder)
        print(f"\n총 {len(image_paths)}장의 이미지를 찾았습니다.")

        # 가게 정보 입력
        restaurant_name, restaurant_location = input_restaurant_info()

        # 맛집 포인트 입력
        restaurant_points = input_restaurant_points()

        # 체험단 필수 키워드 입력
        required_keywords = input_required_keywords()

        # 말투 프로필 불러오기
        style_profile = load_style_profile()
        profile_name = style_profile.get(
            "profile_name",
            "이름 없는 프로필",
        )

        print(f"\n말투 프로필을 불러왔습니다: {profile_name}")

        # 현재까지 입력된 값 확인
        print("\n입력한 맛집 포인트")

        for index, point in enumerate(
            restaurant_points,
            start=1,
        ):
            print(f"{index}. {point}")

        # 블로그 글 생성
        print("\n사진을 분석하고 블로그 글을 생성하고 있습니다.")
        blog_post = generate_blog_post(
            image_paths=image_paths,
            restaurant_name=restaurant_name,
            restaurant_location=restaurant_location,
            restaurant_points=restaurant_points,
            required_keywords=required_keywords,
            style_profile=style_profile,
        )

        output_folder = PROJECT_ROOT / "output"
        output_folder.mkdir(parents=True, exist_ok=True)

        output_path = output_folder / "blog_post.md"
        output_path.write_text(blog_post, encoding="utf-8")

        print("\n블로그 글 생성이 완료되었습니다.")
        print(f"저장 위치: {output_path}")

        # 옵션을 사용했을 때만 네이버에 입력
        if args.write_naver:
            print("\n네이버 블로그 편집기를 여는 중입니다.")

            fill_naver_blog_draft(
                blog_post=blog_post,
                image_dir=DEFAULT_IMAGE_DIR,
            )

            print("\n제목과 본문 입력이 완료되었습니다.")
            print("내용을 확인한 후 직접 발행해주세요.")

        return 0

    except (FileNotFoundError, ValueError) as error:
        print(f"\n오류: {error}")
        return 1
    