from pathlib import Path


SUPPORTED_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".mp4",
}


def load_image_paths(folder_path: str | Path) -> list[Path]:
    folder = Path(folder_path).expanduser().resolve()

    if not folder.exists():
        raise FileNotFoundError(f"폴더가 존재하지 않습니다: {folder}")

    if not folder.is_dir():
        raise NotADirectoryError(f"폴더 경로가 아닙니다: {folder}")

    image_paths = [
        path
        for path in folder.iterdir()
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
    ]

    return sorted(image_paths)