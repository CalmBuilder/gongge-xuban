"""
@Time       : 2026/09/05
@Author     : zhanglp8181
@File       : youtube_runtime.py
@CallChain  : YouTube 垂直能力适配器 → yt-dlp Python Runtime → YouTube 公开数据
@Description: 在可取消超时边界读取公开视频，保留数字字幕与来源时间轴，并披露有界截断。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from html import unescape
import importlib.util
import math
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, TypeVar

from app.cancellation import TurnCancellationRequested


_DEFAULT_TIMEOUT_SECONDS = 45.0
_MAX_SUBTITLE_BYTES = 2 * 1024 * 1024
_MAX_SUBTITLE_CHARS = 80_000
_MAX_SUBTITLE_SEGMENTS = 2_000
_VTT_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<start>\S+)\s+-->\s+(?P<end>\S+)(?:\s.*)?$"
)
_VTT_TAG_PATTERN = re.compile(r"<[^>]+>")
_T = TypeVar("_T")


class YouTubeRuntimeError(RuntimeError):
    """表示 YouTube 运行时缺失、超时或返回内容不符合契约。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码和不包含 URL、凭据或外部正文的摘要。"""

        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class YouTubeRuntimeStatus:
    """承载配置页可展示的 YouTube 运行时发现结果。"""

    ytdlp_available: bool
    js_runtime: str | None
    message: str


class _QuietYtdlpLogger:
    """阻断 yt-dlp 将外部页面正文和命令细节写入平台普通日志。"""

    def debug(self, _message: str) -> None:
        """忽略 yt-dlp 的调试输出。"""

    def warning(self, _message: str) -> None:
        """忽略 yt-dlp 的警告输出，错误由结构化异常承载。"""

    def error(self, _message: str) -> None:
        """忽略 yt-dlp 的错误正文，避免把外部响应带入平台日志。"""


def youtube_runtime_status() -> YouTubeRuntimeStatus:
    """探测项目 Python 依赖和 JS 解释器，不请求具体 YouTube 视频。"""

    try:
        ytdlp_available = importlib.util.find_spec("yt_dlp") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        ytdlp_available = False
    js_runtime = _find_js_runtime()
    if not ytdlp_available:
        return YouTubeRuntimeStatus(
            ytdlp_available=False,
            js_runtime=js_runtime,
            message="尚未发现 yt-dlp Python Runtime；平台构建依赖安装完成后才可读取 YouTube。",
        )
    if js_runtime is None:
        return YouTubeRuntimeStatus(
            ytdlp_available=True,
            js_runtime=None,
            message="已发现 yt-dlp，但未发现 Deno 或 Node.js；YouTube 当前版本需要 JS Runtime。",
        )
    return YouTubeRuntimeStatus(
        ytdlp_available=True,
        js_runtime=js_runtime,
        message=f"已发现 yt-dlp 和 {js_runtime}；具体视频仍以本次读取结果为准。",
    )


def read_youtube_video(
    url: str,
    *,
    subtitle_languages: Sequence[str],
    include_comments: bool = False,
    comments_limit: int = 20,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """读取公开视频、字幕时间轴及按需评论，区分无数据、截断与 Provider 失败。"""

    youtube_dl, download_error = _load_ytdlp()
    js_options = _js_runtime_options()
    bounded_comments_limit = max(1, min(int(comments_limit), 100))
    output_holder: dict[str, object] = {}
    extractor_args: dict[str, object] = {"player_client": ["android"]}
    if include_comments:
        extractor_args["max_comments"] = [str(bounded_comments_limit)]
    options = {
        "extractor_args": {"youtube": extractor_args},
        **js_options,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _QuietYtdlpLogger(),
        "noplaylist": True,
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": list(subtitle_languages),
        "subtitlesformat": "vtt/best",
        "socket_timeout": _socket_timeout(timeout_seconds),
        "retries": 1,
        "fragment_retries": 1,
        "overwrites": False,
    }
    if include_comments:
        # 与 Agent-Reach video.md 的 `--write-comments` 语义一致；只放进内存 info，
        # 不写 info.json，避免把用户任务中的外部评论落到本地文件。
        options["getcomments"] = True

    def operation() -> dict[str, object]:
        """在临时目录读取字幕，连同原始时间轴和截断标记返回且始终清理文件。"""

        temp_dir = Path(tempfile.mkdtemp(prefix="gongge-youtube-"))
        try:
            last_download_error: BaseException | None = None
            for index, language in enumerate(subtitle_languages):
                language_dir = temp_dir / str(index)
                language_dir.mkdir()
                language_options = {
                    **options,
                    "outtmpl": {"default": str(language_dir / "%(id)s.%(language)s.%(ext)s")},
                    "subtitleslangs": [language],
                }
                try:
                    with youtube_dl(language_options) as downloader:
                        output_holder["close"] = downloader.close
                        info = downloader.extract_info(url, download=True)
                    if not isinstance(info, Mapping):
                        raise YouTubeRuntimeError(
                            "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                            "YouTube 返回了无法识别的视频信息。",
                        )
                    subtitle = _read_subtitle(
                        language_dir,
                        (language,),
                    )
                    subtitle_metadata = bool(info.get("subtitles") or info.get("automatic_captions"))
                    if subtitle["content"]:
                        return {
                            "info": dict(info),
                            **{f"subtitle_{key}": value for key, value in subtitle.items()},
                            "subtitle_result_state": "success_with_data",
                        }
                    if not subtitle_metadata:
                        return {
                            "info": dict(info),
                            "subtitle_content": "",
                            "subtitle_language": "",
                            "subtitle_result_state": "no_results",
                        }
                except YouTubeRuntimeError:
                    raise
                except download_error as exc:
                    last_download_error = exc
                    continue
            if last_download_error is not None:
                raise _download_error("YouTube 字幕读取失败。", last_download_error)
            raise YouTubeRuntimeError(
                "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                "YouTube 已报告字幕可用，但字幕内容为空。",
            )
        except YouTubeRuntimeError:
            raise
        except download_error as exc:
            raise _download_error("YouTube 视频读取失败。", exc) from exc
        except (OSError, ValueError, TypeError) as exc:
            raise YouTubeRuntimeError(
                "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                "YouTube 返回内容无法处理。",
            ) from exc
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    return _run_controlled(
        operation,
        timeout_seconds=timeout_seconds,
        is_cancelled=is_cancelled,
        close_holder=output_holder,
    )


def search_youtube_videos(
    query: str,
    *,
    limit: int,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    is_cancelled: Callable[[], bool] | None = None,
) -> list[dict[str, object]]:
    """通过 yt-dlp 的 YouTube 搜索入口读取有界公开视频元数据。"""

    youtube_dl, download_error = _load_ytdlp()
    js_options = _js_runtime_options()
    output_holder: dict[str, object] = {}
    options = {
        "extractor_args": {"youtube": {"player_client": ["android"]}},
        **js_options,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _QuietYtdlpLogger(),
        "extract_flat": True,
        "skip_download": True,
        "playlistend": limit,
        "socket_timeout": _socket_timeout(timeout_seconds),
        "retries": 1,
    }

    def operation() -> list[dict[str, object]]:
        """执行一次固定数量的公开视频搜索并提取条目。"""

        try:
            with youtube_dl(options) as downloader:
                output_holder["close"] = downloader.close
                info = downloader.extract_info(f"ytsearch{limit}:{query}", download=False)
            if not isinstance(info, Mapping):
                raise YouTubeRuntimeError(
                    "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                    "YouTube 搜索返回了无法识别的内容。",
                )
            entries = info.get("entries")
            if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
                raise YouTubeRuntimeError(
                    "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                    "YouTube 搜索没有返回条目集合。",
                )
            return [dict(item) for item in entries[:limit] if isinstance(item, Mapping)]
        except YouTubeRuntimeError:
            raise
        except download_error as exc:
            raise _download_error("YouTube 搜索失败。", exc) from exc
        except (OSError, ValueError, TypeError) as exc:
            raise YouTubeRuntimeError(
                "VERTICAL_ABILITY_PROVIDER_RESPONSE_INVALID",
                "YouTube 搜索返回内容无法处理。",
            ) from exc

    return _run_controlled(
        operation,
        timeout_seconds=timeout_seconds,
        is_cancelled=is_cancelled,
        close_holder=output_holder,
    )


def _load_ytdlp() -> tuple[Any, type[BaseException]]:
    """延迟加载 yt-dlp，确保运行时缺失只影响 YouTube 而不影响平台启动。"""

    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except (ImportError, ModuleNotFoundError) as exc:
        raise YouTubeRuntimeError(
            "YOUTUBE_RUNTIME_NOT_INSTALLED",
            "YouTube 读取需要平台部署 yt-dlp Python Runtime。",
        ) from exc
    return YoutubeDL, DownloadError


def _find_js_runtime() -> str | None:
    """优先使用随包 Node.js，再查找 Deno 或系统 Runtime。"""

    runtime = _find_js_runtime_path()
    if runtime is not None:
        return runtime[0]
    return None


def _js_runtime_options() -> dict[str, object]:
    """把发现到的 JS 解释器显式传给 yt-dlp，避免 Node 隐式配置差异。"""

    runtime = _find_js_runtime_path()
    if runtime is not None:
        runtime_name, runtime_path = runtime
        runtime_key = "deno" if runtime_name == "Deno" else "node"
        return {"js_runtimes": {runtime_key: {"path": runtime_path}}}
    raise YouTubeRuntimeError(
        "YOUTUBE_JS_RUNTIME_MISSING",
        "YouTube 读取需要平台部署 Deno 或 Node.js JS Runtime。",
    )


def _find_js_runtime_path() -> tuple[str, str] | None:
    """返回项目随包或系统可执行的 JS Runtime 名称与绝对路径。"""

    for runtime_name in ("node", "deno"):
        for candidate in _bundled_runtime_candidates(runtime_name):
            if _is_executable_file(candidate):
                return _display_runtime_name(runtime_name), str(candidate)
    for runtime_name in ("deno", "node"):
        system_path = shutil.which(runtime_name)
        if system_path:
            return _display_runtime_name(runtime_name), str(Path(system_path).resolve())
    return None


def _bundled_runtime_candidates(runtime_name: str) -> tuple[Path, ...]:
    """枚举桌面包 runtime/youtube-js 下的跨平台 JS Runtime 路径。"""

    executable = Path(sys.executable).resolve()
    roots: list[Path] = [executable.parent]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(str(meipass)))
    if sys.platform == "darwin" and len(executable.parents) >= 2:
        roots.append(executable.parents[1] / "Resources")
    configured = os.environ.get("GONGGE_XUBAN_YOUTUBE_JS_PATH", "").strip()
    candidates: list[Path] = []
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_dir():
            candidates.extend(
                (configured_path / runtime_name, configured_path / f"{runtime_name}.exe")
            )
        else:
            candidates.append(configured_path)
    for root in roots:
        runtime_root = root / "runtime" / "youtube-js"
        candidates.extend(
            (
                runtime_root / runtime_name,
                runtime_root / f"{runtime_name}.exe",
            )
        )
    return tuple(dict.fromkeys(candidates))


def _display_runtime_name(runtime_name: str) -> str:
    """把内部 Runtime slug 转成配置页和诊断使用的稳定名称。"""

    return "Deno" if runtime_name == "deno" else "Node.js"


def _is_executable_file(path: Path) -> bool:
    """判断候选 Runtime 是普通文件且具备当前平台执行权限。"""

    return path.is_file() and os.access(path, os.X_OK)


def _socket_timeout(timeout_seconds: float) -> int:
    """把平台总超时转换为 yt-dlp 单次网络套接字超时。"""

    return max(5, min(int(max(float(timeout_seconds), 5.0)), 120))


def _run_controlled(
    operation: Callable[[], _T],
    *,
    timeout_seconds: float,
    is_cancelled: Callable[[], bool] | None,
    close_holder: dict[str, object],
) -> _T:
    """在线程边界内执行 yt-dlp，并统一处理取消、总超时和异常传播。"""

    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def worker() -> None:
        """执行 yt-dlp 任务并把结果送回控制线程。"""

        try:
            result_queue.put_nowait((True, operation()))
        except BaseException as exc:
            try:
                result_queue.put_nowait((False, exc))
            except queue.Full:
                pass

    threading.Thread(target=worker, name="youtube-runtime", daemon=True).start()
    deadline = time.monotonic() + max(float(timeout_seconds), 1.0)
    while True:
        if is_cancelled is not None and is_cancelled():
            _close_ytdlp(close_holder)
            raise TurnCancellationRequested("当前 Turn 已取消。")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _close_ytdlp(close_holder)
            raise YouTubeRuntimeError("VERTICAL_ABILITY_PROVIDER_TIMEOUT", "YouTube 请求超时。")
        try:
            succeeded, value = result_queue.get(timeout=min(0.05, remaining))
        except queue.Empty:
            continue
        if succeeded:
            return value  # type: ignore[return-value]
        if isinstance(value, BaseException):
            raise value
        raise YouTubeRuntimeError(
            "VERTICAL_ABILITY_EXECUTION_ERROR",
            "YouTube Runtime 返回了未知结果。",
        )


def _close_ytdlp(holder: Mapping[str, object]) -> None:
    """尝试关闭正在运行的 yt-dlp 会话，取消后不等待外部线程。"""

    closer = holder.get("close")
    if callable(closer):
        try:
            closer()
        except BaseException:
            pass


def _download_error(prefix: str, error: BaseException) -> YouTubeRuntimeError:
    """将 yt-dlp 的具体错误收敛为不含外部正文的 Provider 失败。"""

    del error
    return YouTubeRuntimeError("VERTICAL_ABILITY_PROVIDER_UNAVAILABLE", prefix)


def _read_subtitle(directory: Path, languages: Sequence[str]) -> dict[str, object]:
    """从临时目录选择语言优先级最高的 VTT，保留正文、有效时间轴与截断标记。"""

    files = [path for path in directory.rglob("*.vtt") if path.is_file()]
    files.sort(key=lambda path: _subtitle_file_rank(path, languages))
    for path in files:
        raw = path.read_bytes()
        if len(raw) > _MAX_SUBTITLE_BYTES:
            raise YouTubeRuntimeError(
                "VERTICAL_ABILITY_RESPONSE_TOO_LARGE",
                "YouTube 字幕响应超过安全上限。",
            )
        subtitle = parse_vtt_subtitle(raw.decode("utf-8", errors="replace"))
        if subtitle["content"]:
            return {**subtitle, "language": _subtitle_language(path, languages)}
    return {"content": "", "language": ""}


def _subtitle_file_rank(path: Path, languages: Sequence[str]) -> tuple[int, str]:
    """按请求语言顺序选择字幕文件，并用文件名消除并列漂移。"""

    filename = path.name.casefold()
    for index, language in enumerate(languages):
        if str(language).casefold() in filename:
            return index, filename
    return len(languages), filename


def _subtitle_language(path: Path, languages: Sequence[str]) -> str:
    """从字幕文件名提取匹配的语言标签，找不到时返回 unknown。"""

    filename = path.name.casefold()
    for language in languages:
        if str(language).casefold() in filename:
            return str(language)
    return "unknown"


def _clean_vtt(text: str) -> str:
    """兼容纯文本调用方，只清理 cue 编号与标签，保留数字台词并去连续重复行。"""

    return str(parse_vtt_subtitle(text)["content"])


def parse_vtt_subtitle(text: str) -> dict[str, object]:
    """依据 VTT 块结构识别 cue 与元数据，保留有时间的重复台词，不为纯文本造时间。"""

    def segments() -> Iterable[Mapping[str, object]]:
        """按块生成来源分段，NOTE/STYLE/REGION 块不混入用户台词。"""

        blocks = re.split(r"\n\s*\n", text.lstrip("\ufeff").replace("\r\n", "\n"))
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if not lines:
                continue
            heading = lines[0].split(maxsplit=1)[0].upper()
            if heading in {"WEBVTT", "NOTE", "STYLE", "REGION"}:
                continue
            timing = None
            body = lines
            for index, line in enumerate(lines[:2]):
                if "-->" in line:
                    timing = _VTT_TIMESTAMP_PATTERN.fullmatch(line)
                    body = lines[index + 1:]
                    break
            content = "\n".join(
                " ".join(unescape(_VTT_TAG_PATTERN.sub("", line)).split()) for line in body
            ).strip()
            if content:
                yield {
                    "text": content,
                    **(
                        {"start": timing["start"], "end": timing["end"]}
                        if timing else {}
                    ),
                }

    return normalize_subtitle_segments(segments(), deduplicate_text=True)


def _subtitle_seconds(value: object) -> float | None:
    """接受有限秒数、OpenCLI 秒后缀或时分秒，不把未知时间或布尔值解释为零。"""

    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r"\d+(?:\.\d+)?s", value):
            value = value[:-1]
    if isinstance(value, str) and ":" in value:
        if not re.fullmatch(r"(?:\d{2,}:)?[0-5]\d:[0-5]\d(?:\.\d+)?", value):
            return None
        parts = [float(part) for part in value.split(":")]
        seconds = sum(part * 60 ** index for index, part in enumerate(reversed(parts)))
    else:
        try:
            seconds = float(value)
        except (ValueError, OverflowError):
            return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def normalize_subtitle_segments(
    segments: Iterable[Mapping[str, object]],
    *,
    deduplicate_text: bool = False,
) -> dict[str, object]:
    """有界归一化字幕正文和真实分段，分别披露字符/分段截断、无效及无时间台词数。"""

    parts: list[str] = []
    timeline: list[dict[str, object]] = []
    previous = ""
    content_length = 0
    timeline_length = 0
    content_truncated = False
    timeline_truncated = False
    invalid_count = 0
    untimed_count = 0
    for segment in segments:
        text = segment.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        text = text.strip()
        for line in text.splitlines():
            if not line or (deduplicate_text and line == previous):
                continue
            previous = line
            prefix = "\n" if parts else ""
            value = prefix + line
            remaining = max(0, _MAX_SUBTITLE_CHARS - content_length)
            if len(value) > remaining:
                content_truncated = True
            if remaining:
                parts.append(value[:remaining])
                content_length += len(parts[-1])
        if "start" not in segment and "end" not in segment:
            untimed_count += 1
            continue
        start = _subtitle_seconds(segment.get("start"))
        end = _subtitle_seconds(segment.get("end"))
        if start is None or end is None or end < start:
            invalid_count += 1
            continue
        remaining = max(0, _MAX_SUBTITLE_CHARS - timeline_length)
        if not remaining or len(timeline) >= _MAX_SUBTITLE_SEGMENTS:
            timeline_truncated = True
            continue
        cue: dict[str, object] = {"start": start, "end": end, "text": text[:remaining]}
        if len(text) > remaining:
            timeline_truncated = True
            cue["text_truncated"] = True
        timeline.append(cue)
        timeline_length += len(str(cue["text"]))
    result: dict[str, object] = {
        "content": "".join(parts).strip(),
        "timeline": timeline,
        "content_truncated": content_truncated,
        "timeline_truncated": timeline_truncated,
    }
    if invalid_count:
        result["timeline_invalid_count"] = invalid_count
    if untimed_count:
        result["timeline_untimed_count"] = untimed_count
    return result
