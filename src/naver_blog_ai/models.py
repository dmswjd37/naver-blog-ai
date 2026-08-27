from typing import TypedDict


class RequiredKeyword(TypedDict):
    keyword: str
    title_required: bool
    body_min_count: int