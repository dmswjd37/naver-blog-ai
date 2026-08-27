from __future__ import annotations

import queue
import socket
import subprocess
import threading
import traceback
import tkinter as tk
import sys

from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Literal
from contextlib import redirect_stdout, redirect_stderr

from naver_blog_ai.naver_writer import fill_naver_blog_draft
from naver_blog_ai.workflow import RequiredKeyword, create_blog_post
from naver_blog_ai.paths import PROJECT_ROOT


DEFAULT_IMAGE_DIR = PROJECT_ROOT / "img"
DEFAULT_BLOG_POST = PROJECT_ROOT / "output" / "blog_post.md"
START_CHROME_BAT = PROJECT_ROOT / "start_naver_chrome.bat"

CHROME_HOST = "127.0.0.1"
CHROME_DEBUG_PORT = 9222
CHROME_WAIT_SECONDS = 30

EventKind = Literal["log", "success", "error", "finished", "output_path"]


class QueuePrintWriter:
    """print() 내용을 작업 로그 이벤트로 전달한다."""

    def __init__(self, event_queue: Any) -> None:
        self.event_queue = event_queue
        self.buffer = ""

    def write(self, text: str) -> int:
        self.buffer += text

        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)

            if line.strip():
                self.event_queue.put(("log", line))

        return len(text)

    def flush(self) -> None:
        if self.buffer.strip():
            self.event_queue.put(
                ("log", self.buffer.rstrip())
            )
            self.buffer = ""


@dataclass(frozen=True)
class GenerationForm:
    image_dir: Path
    output_path: Path
    restaurant_name: str
    restaurant_location: str
    restaurant_points: list[str]
    required_keywords: list[RequiredKeyword]


class NaverBlogApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()

        self.title("네이버 블로그 AI")
        self.geometry("900x780")
        self.minsize(780, 680)

        self.event_queue: queue.Queue[tuple[EventKind, Any]] = queue.Queue()
        self.is_working = False

        self.image_dir = tk.StringVar(value=str(DEFAULT_IMAGE_DIR))
        self.output_path = tk.StringVar(value=str(DEFAULT_BLOG_POST))
        self.restaurant_name = tk.StringVar()
        self.restaurant_location = tk.StringVar()
        self.status_text = tk.StringVar(value="작업 대기 중")

        self._create_widgets()
        self.after(100, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _create_widgets(self) -> None:
        container = ttk.Frame(self, padding=16)
        container.pack(fill="both", expand=True)

        input_frame = ttk.LabelFrame(
            container,
            text="블로그 글 생성 정보",
            padding=12,
        )
        input_frame.pack(fill="x", pady=(0, 12))
        input_frame.columnconfigure(1, weight=1)

        ttk.Label(input_frame, text="이미지 폴더").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(input_frame, textvariable=self.image_dir).grid(
            row=0, column=1, sticky="ew", pady=4
        )
        ttk.Button(
            input_frame,
            text="폴더 선택",
            command=self._select_image_dir,
        ).grid(row=0, column=2, padx=(8, 0), pady=4)

        ttk.Label(input_frame, text="결과 파일").grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(input_frame, textvariable=self.output_path).grid(
            row=1, column=1, sticky="ew", pady=4
        )
        ttk.Button(
            input_frame,
            text="파일 선택",
            command=self._select_output_path,
        ).grid(row=1, column=2, padx=(8, 0), pady=4)

        ttk.Label(input_frame, text="가게명").grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(input_frame, textvariable=self.restaurant_name).grid(
            row=2, column=1, columnspan=2, sticky="ew", pady=4
        )

        ttk.Label(input_frame, text="지역 또는 주소").grid(
            row=3, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(input_frame, textvariable=self.restaurant_location).grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=4
        )

        ttk.Label(
            input_frame,
            text="맛집 포인트\n(한 줄에 하나)",
        ).grid(row=4, column=0, sticky="nw", padx=(0, 10), pady=4)
        self.points_text = tk.Text(input_frame, height=7, wrap="word")
        self.points_text.grid(
            row=4, column=1, columnspan=2, sticky="ew", pady=4
        )

        ttk.Label(
            input_frame,
            text="필수 키워드",
        ).grid(row=5, column=0, sticky="nw", padx=(0, 10), pady=4)

        keyword_frame = ttk.Frame(input_frame)
        keyword_frame.grid(
            row=5, column=1, columnspan=2, sticky="ew", pady=4
        )
        keyword_frame.columnconfigure(0, weight=1)

        self.keywords_text = tk.Text(keyword_frame, height=5, wrap="none")
        self.keywords_text.grid(row=0, column=0, sticky="ew")
        self.keywords_text.insert(
            "1.0",
            "# 형식: 키워드 | 제목필수(Y/N) | 본문최소횟수\n"
            "# 예: 의정부 초밥 맛집 | Y | 3",
        )

        ttk.Label(
            keyword_frame,
            text="주석(#)은 무시됩니다. 예: 의정부 초밥 맛집 | Y | 3",
            foreground="#666666",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        action_frame = ttk.Frame(container)
        action_frame.pack(fill="x", pady=(0, 12))

        self.chrome_button = ttk.Button(
            action_frame,
            text="1. Chrome 열기",
            command=self._start_open_chrome,
        )
        self.chrome_button.pack(side="left", padx=(0, 8))

        self.generate_button = ttk.Button(
            action_frame,
            text="2. 블로그 글 생성",
            command=self._start_generation,
        )
        self.generate_button.pack(side="left", padx=(0, 8))

        self.write_button = ttk.Button(
            action_frame,
            text="3. 네이버에 작성",
            command=self._start_write_only,
        )
        self.write_button.pack(side="left", padx=(0, 8))

        self.all_button = ttk.Button(
            action_frame,
            text="전체 자동 실행",
            command=self._start_all,
        )
        self.all_button.pack(side="left")

        status_frame = ttk.Frame(container)
        status_frame.pack(fill="x", pady=(0, 12))

        self.progress_bar = ttk.Progressbar(
            status_frame,
            mode="indeterminate",
        )
        self.progress_bar.pack(fill="x", pady=(0, 6))

        ttk.Label(
            status_frame,
            textvariable=self.status_text,
        ).pack(anchor="e")

        log_frame = ttk.LabelFrame(container, text="작업 로그", padding=8)
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            state="disabled",
        )
        self.log_text.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(
            log_frame,
            orient="vertical",
            command=self.log_text.yview,
        )
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _select_image_dir(self) -> None:
        selected = filedialog.askdirectory(
            title="이미지 폴더 선택",
            initialdir=self.image_dir.get() or str(PROJECT_ROOT),
        )
        if selected:
            self.image_dir.set(selected)

    def _select_output_path(self) -> None:
        selected = filedialog.asksaveasfilename(
            title="블로그 글 저장 위치",
            initialdir=DEFAULT_BLOG_POST.parent,
            initialfile=DEFAULT_BLOG_POST.name,
            defaultextension=".md",
            filetypes=[
                ("Markdown 파일", "*.md"),
                ("텍스트 파일", "*.txt"),
                ("모든 파일", "*.*"),
            ],
        )
        if selected:
            self.output_path.set(selected)

    @staticmethod
    def _nonempty_lines(value: str) -> list[str]:
        return [line.strip() for line in value.splitlines() if line.strip()]

    def _parse_required_keywords(self) -> list[RequiredKeyword]:
        lines = self._nonempty_lines(self.keywords_text.get("1.0", "end"))
        keywords: list[RequiredKeyword] = []

        for line_number, line in enumerate(lines, start=1):
            if line.startswith("#"):
                continue

            parts = [part.strip() for part in line.split("|")]
            if len(parts) != 3:
                raise ValueError(
                    f"필수 키워드 {line_number}번째 줄의 형식이 잘못됐습니다.\n"
                    "형식: 키워드 | Y 또는 N | 본문 최소 횟수"
                )

            keyword, title_value, body_count_value = parts
            if not keyword:
                raise ValueError(
                    f"필수 키워드 {line_number}번째 줄의 키워드가 비어 있습니다."
                )

            normalized_title_value = title_value.upper()
            if normalized_title_value not in {"Y", "N"}:
                raise ValueError(
                    f"필수 키워드 {line_number}번째 줄은 제목 필수 여부를 "
                    "Y 또는 N으로 입력해야 합니다."
                )

            try:
                body_min_count = int(body_count_value)
            except ValueError as error:
                raise ValueError(
                    f"필수 키워드 {line_number}번째 줄의 본문 횟수는 정수여야 합니다."
                ) from error

            if body_min_count < 0:
                raise ValueError("본문 최소 횟수는 0 이상이어야 합니다.")

            keywords.append(
                {
                    "keyword": keyword,
                    "title_required": normalized_title_value == "Y",
                    "body_min_count": body_min_count,
                }
            )

        return keywords

    def _collect_generation_form(self) -> GenerationForm:
        image_dir = Path(self.image_dir.get().strip()).resolve()
        output_path = Path(self.output_path.get().strip()).resolve()
        restaurant_name = self.restaurant_name.get().strip()
        restaurant_location = self.restaurant_location.get().strip()
        restaurant_points = self._nonempty_lines(
            self.points_text.get("1.0", "end")
        )

        if not image_dir.is_dir():
            raise ValueError(f"이미지 폴더를 찾을 수 없습니다.\n{image_dir}")
        if not output_path.name:
            raise ValueError("결과 파일 경로를 입력해주세요.")
        if not restaurant_name:
            raise ValueError("가게명을 입력해주세요.")
        if not restaurant_points:
            raise ValueError("맛집 포인트를 하나 이상 입력해주세요.")

        return GenerationForm(
            image_dir=image_dir,
            output_path=output_path,
            restaurant_name=restaurant_name,
            restaurant_location=restaurant_location,
            restaurant_points=restaurant_points,
            required_keywords=self._parse_required_keywords(),
        )

    def _collect_write_form(self) -> tuple[Path, Path]:
        image_dir = Path(self.image_dir.get().strip()).resolve()
        output_path = Path(self.output_path.get().strip()).resolve()

        if not image_dir.is_dir():
            raise ValueError(f"이미지 폴더를 찾을 수 없습니다.\n{image_dir}")
        if not output_path.is_file():
            raise ValueError(f"블로그 글 파일을 찾을 수 없습니다.\n{output_path}")

        return image_dir, output_path

    def _begin_work(self, status: str) -> None:
        self.is_working = True
        self.status_text.set(status)
        self.progress_bar.start(10)

        for button in self._action_buttons():
            button.configure(state="disabled")

    def _finish_work(self) -> None:
        self.is_working = False
        self.progress_bar.stop()
        self.status_text.set("작업 대기 중")

        for button in self._action_buttons():
            button.configure(state="normal")

    def _action_buttons(self) -> tuple[ttk.Button, ...]:
        return (
            self.chrome_button,
            self.generate_button,
            self.write_button,
            self.all_button,
        )

    def _start_worker(
        self,
        status: str,
        target: Any,
        *args: Any,
    ) -> None:
        if self.is_working:
            return

        self._begin_work(status)
        threading.Thread(
            target=self._worker_wrapper,
            args=(target, *args),
            daemon=True,
        ).start()

    def _worker_wrapper(self, target: Any, *args: Any) -> None:
        print_writer = QueuePrintWriter(self.event_queue)

        try:
            with (
                redirect_stdout(print_writer),
                redirect_stderr(print_writer),
            ):
                target(*args)

        except Exception as error:
            self.event_queue.put(("log", traceback.format_exc()))
            self.event_queue.put(("error", str(error)))

        finally:
            print_writer.flush()
            self.event_queue.put(("finished", None))

    def _start_open_chrome(self) -> None:
        self._start_worker("Chrome 실행 중", self._run_open_chrome)

    def _start_generation(self) -> None:
        try:
            form = self._collect_generation_form()
        except Exception as error:
            messagebox.showerror("입력 오류", str(error))
            return

        self._start_worker("블로그 글 생성 중", self._run_generation, form)

    def _start_write_only(self) -> None:
        try:
            image_dir, output_path = self._collect_write_form()
        except Exception as error:
            messagebox.showerror("입력 오류", str(error))
            return

        self._start_worker(
            "네이버 블로그 작성 중",
            self._run_write_only,
            image_dir,
            output_path,
        )

    def _start_all(self) -> None:
        try:
            form = self._collect_generation_form()
        except Exception as error:
            messagebox.showerror("입력 오류", str(error))
            return

        self._start_worker("전체 자동 실행 중", self._run_all, form)

    def _run_open_chrome(self) -> None:
        self._ensure_naver_chrome()
        self.event_queue.put(("success", "Chrome 실행 준비가 완료됐습니다."))

    def _run_generation(self, form: GenerationForm) -> None:
        self.event_queue.put(("log", "첨부 이미지를 확인합니다."))
        self.event_queue.put(("log", "Codex로 블로그 글을 생성합니다."))

        output_path = create_blog_post(
            image_dir=form.image_dir,
            output_path=form.output_path,
            restaurant_name=form.restaurant_name,
            restaurant_location=form.restaurant_location,
            restaurant_points=form.restaurant_points,
            required_keywords=form.required_keywords,
        )

        self.event_queue.put(("output_path", str(output_path)))
        self.event_queue.put(("log", f"블로그 글 저장 완료: {output_path}"))
        self.event_queue.put(("success", "블로그 글을 생성했습니다."))

    def _run_write_only(self, image_dir: Path, output_path: Path) -> None:
        self._ensure_naver_chrome()
        self.event_queue.put(("log", f"블로그 글을 불러옵니다: {output_path}"))

        blog_post = output_path.read_text(encoding="utf-8")
        self.event_queue.put(("log", "네이버 블로그 편집기를 여는 중입니다."))

        fill_naver_blog_draft(
            blog_post=blog_post,
            image_dir=image_dir,
        )

        self.event_queue.put(("success", "네이버 블로그 입력이 완료됐습니다."))

    def _run_all(self, form: GenerationForm) -> None:
        self._ensure_naver_chrome()
        self.event_queue.put(("log", "블로그 글을 생성합니다."))

        output_path = create_blog_post(
            image_dir=form.image_dir,
            output_path=form.output_path,
            restaurant_name=form.restaurant_name,
            restaurant_location=form.restaurant_location,
            restaurant_points=form.restaurant_points,
            required_keywords=form.required_keywords,
        )

        self.event_queue.put(("output_path", str(output_path)))
        self.event_queue.put(("log", f"블로그 글 저장 완료: {output_path}"))

        blog_post = output_path.read_text(encoding="utf-8")
        self.event_queue.put(("log", "네이버 블로그 편집기에 입력합니다."))

        fill_naver_blog_draft(
            blog_post=blog_post,
            image_dir=form.image_dir,
        )

        self.event_queue.put(("success", "글 생성과 네이버 입력이 완료됐습니다."))

    def _ensure_naver_chrome(self) -> None:
        if self._is_chrome_debugging_ready():
            self.event_queue.put(("log", "이미 실행 중인 네이버 Chrome을 사용합니다."))
            return

        if not START_CHROME_BAT.is_file():
            raise FileNotFoundError(
                f"Chrome 실행 파일을 찾을 수 없습니다.\n{START_CHROME_BAT}"
            )

        self.event_queue.put(("log", f"Chrome을 실행합니다: {START_CHROME_BAT}"))

        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(
            subprocess, "CREATE_NO_WINDOW"
        ) else 0

        subprocess.Popen(
            ["cmd.exe", "/c", str(START_CHROME_BAT)],
            cwd=PROJECT_ROOT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        for _ in range(CHROME_WAIT_SECONDS * 2):
            if self._is_chrome_debugging_ready():
                self.event_queue.put(("log", "Chrome 원격 디버깅 연결을 확인했습니다."))
                return
            threading.Event().wait(0.5)

        raise RuntimeError(
            "Chrome은 실행했지만 9222 포트에 연결하지 못했습니다.\n"
            "start_naver_chrome.bat에 --remote-debugging-port=9222가 있는지 확인해주세요."
        )

    @staticmethod
    def _is_chrome_debugging_ready() -> bool:
        try:
            with socket.create_connection(
                (CHROME_HOST, CHROME_DEBUG_PORT),
                timeout=0.5,
            ):
                return True
        except OSError:
            return False

    def _poll_events(self) -> None:
        while True:
            try:
                kind, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                self._append_log(str(payload))
            elif kind == "output_path":
                self.output_path.set(str(payload))
            elif kind == "success":
                self._append_log(str(payload))
                messagebox.showinfo("완료", str(payload))
            elif kind == "error":
                self._append_log(f"오류: {payload}")
                messagebox.showerror("오류", str(payload))
            elif kind == "finished":
                self._finish_work()

        self.after(100, self._poll_events)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.is_working:
            should_close = messagebox.askyesno(
                "종료 확인",
                "현재 작업이 진행 중입니다. GUI를 종료할까요?",
            )
            if not should_close:
                return

        self.destroy()


def main() -> None:
    app = NaverBlogApp()
    app.mainloop()


if __name__ == "__main__":
    main()
