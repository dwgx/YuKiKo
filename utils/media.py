"""共享媒体处理工具 — 下载 / FFmpeg / faster-whisper 转写 / 音视频分析。

提供统一的媒体处理基础设施，供 agent_tools / video_analyzer / engine 共用。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from utils.process_compat import macos_subprocess_kwargs, resolve_executable_for_spawn

_log = logging.getLogger("yukiko.media")

# ---------------------------------------------------------------------------
# FFmpeg 工具
# ---------------------------------------------------------------------------

_ffmpeg_bin: str | None = None
_ffprobe_bin: str | None = None


def _find_bin(name: str) -> str | None:
    """查找可执行文件路径。"""
    found = shutil.which(name)
    if found:
        return found
    for candidate in (
        Path(os.environ.get("FFMPEG_HOME", "")) / name,
        Path(os.environ.get("FFMPEG_HOME", "")) / "bin" / name,
    ):
        if candidate.is_file():
            return str(candidate)

    lower_name = name.lower()
    if lower_name in {"ffmpeg", "ffmpeg.exe"}:
        try:
            import imageio_ffmpeg  # type: ignore

            bundled = imageio_ffmpeg.get_ffmpeg_exe()
            if bundled and Path(bundled).is_file():
                return str(Path(bundled))
        except Exception:
            pass

    if lower_name in {"ffprobe", "ffprobe.exe"}:
        ffmpeg_path = _find_bin("ffmpeg")
        if ffmpeg_path:
            ffmpeg_exe = Path(ffmpeg_path)
            sibling = ffmpeg_exe.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
            if sibling.is_file():
                return str(sibling)
    return None


def get_ffmpeg() -> str | None:
    global _ffmpeg_bin
    if _ffmpeg_bin is None:
        _ffmpeg_bin = _find_bin("ffmpeg") or _find_bin("ffmpeg.exe") or ""
    return _ffmpeg_bin or None


def get_ffprobe() -> str | None:
    global _ffprobe_bin
    if _ffprobe_bin is None:
        _ffprobe_bin = _find_bin("ffprobe") or _find_bin("ffprobe.exe") or ""
    return _ffprobe_bin or None


async def run_ffmpeg(
    args: list[str],
    *,
    timeout: float = 60.0,
    cwd: str | Path | None = None,
) -> tuple[bool, str]:
    """异步执行 ffmpeg 命令，返回 (success, stderr_output)。"""
    ffmpeg = get_ffmpeg()
    if not ffmpeg:
        return False, "ffmpeg not found"
    ffmpeg = resolve_executable_for_spawn(ffmpeg)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning"] + args
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            **macos_subprocess_kwargs(),
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        ok = proc.returncode == 0
        return ok, stderr.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        return False, "ffmpeg timeout"
    except Exception as exc:
        return False, f"ffmpeg error: {exc}"


async def run_ffprobe_json(
    file_path: str | Path,
    *,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """用 ffprobe 获取媒体文件的 JSON 元数据。"""
    ffprobe = get_ffprobe()
    if not ffprobe:
        return {}
    ffprobe = resolve_executable_for_spawn(ffprobe)
    cmd = [
        ffprobe, "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(file_path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **macos_subprocess_kwargs(),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            return {}
        import json
        return json.loads(stdout.decode("utf-8", errors="replace"))
    except Exception:
        return {}



def sniff_audio_container(path: str | Path) -> str:
    """读文件头判断音频容器，返回小写容器名，认不出返回空串。

    QQ 语音是腾讯魔改的 SILK v3（可选 1 字节 `0x02` 前缀 + `#!SILK_V3`），
    ffmpeg 既没有 silk demuxer 也没有 silk decoder，靠扩展名或 mime 猜是错的：
    落盘成 `.mp3` 只会让 ffmpeg 报 `Format mp3 detected only with low score of 1`
    然后失败。所以这里只看字节，不看后缀。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except Exception:
        return ""
    if not head:
        return ""
    # 腾讯 SILK 前缀：首字节 0x02 之后才是标准魔数（对齐 pilk 的处理）。
    silk_body = head[1:] if head[:1] == b"\x02" else head
    if silk_body.startswith(b"#!SILK"):
        return "silk"
    if head.startswith(b"#!AMR"):
        return "amr"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"fLaC"):
        return "flac"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head.startswith(b"ID3") or (head[:1] == b"\xff" and head[1:2] in (b"\xfb", b"\xf3", b"\xf2")):
        return "mp3"
    return ""


def _classify_ffmpeg_error(err: str) -> str:
    """把 ffmpeg stderr 归一成短原因码 —— stderr 原文不能进 display。"""
    lowered = err.lower()
    if "not found" in lowered:
        return "ffmpeg_missing"
    if "timeout" in lowered:
        return "ffmpeg_timeout"
    return "ffmpeg_decode_failed"


async def decode_silk_to_wav(
    silk_path: str | Path,
    output_path: str | Path,
    *,
    sample_rate: int = 16000,
    timeout: float = 30.0,
) -> tuple[str | None, str]:
    """把腾讯 SILK v3 解成 WAV，返回 `(wav 路径, reason)`。

    成功时 reason 为空；失败时路径为 None，reason 取值
    `pilk_missing` / `not_silk` / `pilk_decode_failed` / `empty_pcm` /
    `wav_write_failed` / `timeout`。

    ffmpeg 帮不上忙（没有 silk demuxer），所以这里走 pilk 这个 C 扩展。
    pilk 是同步调用，丢进 executor 免得阻塞事件循环。
    """
    silk_path = Path(silk_path)
    output_path = Path(output_path)
    try:
        import pilk  # type: ignore
    except Exception:
        _log.warning("silk_decode_failed | reason=pilk_missing | file=%s", silk_path.name)
        return None, "pilk_missing"

    if sniff_audio_container(silk_path) != "silk":
        _log.warning("silk_decode_failed | reason=not_silk | file=%s", silk_path.name)
        return None, "not_silk"

    # pilk 内部解出的是 24kHz 裸 PCM；自己管临时目录，pilk.silk_to_wav 用的
    # tempfile.mktemp 会漏文件。
    tmp_dir = Path(tempfile.mkdtemp(prefix="yukiko-silk-"))
    pcm_path = tmp_dir / "decoded.pcm"
    pcm_rate = 24000
    try:
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: pilk.decode(str(silk_path), str(pcm_path), pcm_rate=pcm_rate),
                ),
                timeout=timeout,
            )
        except TimeoutError:
            _log.warning("silk_decode_failed | reason=timeout | file=%s", silk_path.name)
            return None, "timeout"
        except Exception as exc:
            _log.warning(
                "silk_decode_failed | reason=pilk_decode_failed | file=%s | %s",
                silk_path.name,
                exc,
            )
            return None, "pilk_decode_failed"

        if not (pcm_path.is_file() and pcm_path.stat().st_size > 0):
            _log.warning("silk_decode_failed | reason=empty_pcm | file=%s", silk_path.name)
            return None, "empty_pcm"

        output_path.parent.mkdir(parents=True, exist_ok=True)
        ok, err = await run_ffmpeg(
            [
                "-f", "s16le",
                "-ar", str(pcm_rate),
                "-ac", "1",
                "-i", str(pcm_path),
                "-acodec", "pcm_s16le",
                "-ar", str(sample_rate),
                "-ac", "1",
                str(output_path),
            ],
            timeout=timeout,
        )
        if ok and output_path.is_file() and output_path.stat().st_size > 0:
            return str(output_path), ""

        # ffmpeg 不可用/重采样失败时，用标准库直接封一个 24kHz WAV：
        # 采样率和请求的不一样，但 ASR 侧自己会重采样，比整条断掉好。
        _log.warning(
            "silk_decode_resample_fallback | reason=%s | file=%s | kept_rate=%d",
            _classify_ffmpeg_error(err),
            silk_path.name,
            pcm_rate,
        )
        try:
            import wave

            with wave.open(str(output_path), "wb") as wav_fh:
                wav_fh.setnchannels(1)
                wav_fh.setsampwidth(2)
                wav_fh.setframerate(pcm_rate)
                wav_fh.writeframes(pcm_path.read_bytes())
        except Exception as exc:
            _log.warning(
                "silk_decode_failed | reason=wav_write_failed | file=%s | %s",
                silk_path.name,
                exc,
            )
            return None, "wav_write_failed"
        return str(output_path), ""
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def extract_audio_detailed(
    video_path: str | Path,
    output_path: str | Path | None = None,
    *,
    sample_rate: int = 16000,
    mono: bool = True,
    timeout: float = 60.0,
) -> tuple[str | None, str]:
    """提取 WAV 音频，返回 `(路径, reason)`；成功时 reason 为空。

    先按字节探测容器：SILK 交给 pilk，其余走 ffmpeg。reason 是短码，
    ffmpeg stderr 只进日志，不外泄给上层当用户可见文案。
    """
    video_path = Path(video_path)
    if not video_path.is_file():
        _log.warning("extract_audio_failed | reason=source_missing | file=%s", video_path.name)
        return None, "source_missing"
    if output_path is None:
        output_path = video_path.with_suffix(".wav")
    output_path = Path(output_path)

    if sniff_audio_container(video_path) == "silk":
        wav_path, reason = await decode_silk_to_wav(
            video_path, output_path, sample_rate=sample_rate, timeout=timeout
        )
        return wav_path, reason

    args = [
        "-i", str(video_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", str(sample_rate),
    ]
    if mono:
        args.extend(["-ac", "1"])
    args.append(str(output_path))
    ok, err = await run_ffmpeg(args, timeout=timeout)
    if ok and output_path.is_file():
        return str(output_path), ""
    reason = _classify_ffmpeg_error(err)
    _log.warning(
        "extract_audio_failed | reason=%s | file=%s | %s", reason, video_path.name, err
    )
    return None, reason


async def extract_audio(
    video_path: str | Path,
    output_path: str | Path | None = None,
    *,
    sample_rate: int = 16000,
    mono: bool = True,
    timeout: float = 60.0,
) -> str | None:
    """从视频/音频文件中提取 WAV 音频（Whisper 友好格式）。

    返回输出文件路径，失败返回 None。要失败原因请直接用
    `extract_audio_detailed`。
    """
    wav_path, _reason = await extract_audio_detailed(
        video_path,
        output_path,
        sample_rate=sample_rate,
        mono=mono,
        timeout=timeout,
    )
    return wav_path


async def extract_keyframes(
    video_path: str | Path,
    output_dir: str | Path,
    *,
    max_frames: int = 8,
    interval_seconds: float = 0,
    timeout: float = 60.0,
) -> list[str]:
    """从视频中提取关键帧图片。

    如果 interval_seconds > 0，按固定间隔提取；否则使用场景检测。
    返回提取的图片路径列表。
    """
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if not video_path.is_file():
        return []

    pattern = str(output_dir / "frame_%04d.jpg")

    if interval_seconds > 0:
        args = [
            "-i", str(video_path),
            "-vf", f"fps=1/{interval_seconds}",
            "-frames:v", str(max_frames),
            "-q:v", "3",
            pattern,
        ]
    else:
        # 场景检测 + 均匀采样兜底
        args = [
            "-i", str(video_path),
            "-vf", f"select='gt(scene,0.3)',setpts=N/FRAME_RATE/TB",
            "-frames:v", str(max_frames),
            "-vsync", "vfr",
            "-q:v", "3",
            pattern,
        ]

    ok, err = await run_ffmpeg(args, timeout=timeout)
    frames = sorted(output_dir.glob("frame_*.jpg"))

    # 场景检测可能提取太少，回退到均匀采样
    if len(frames) < 2 and interval_seconds <= 0:
        probe = await run_ffprobe_json(video_path)
        duration = _get_duration(probe)
        if duration > 0:
            step = max(1.0, duration / max_frames)
            args2 = [
                "-i", str(video_path),
                "-vf", f"fps=1/{step}",
                "-frames:v", str(max_frames),
                "-q:v", "3",
                pattern,
            ]
            await run_ffmpeg(args2, timeout=timeout)
            frames = sorted(output_dir.glob("frame_*.jpg"))

    return [str(f) for f in frames[:max_frames]]


def _get_duration(probe: dict[str, Any]) -> float:
    """从 ffprobe JSON 中提取时长（秒）。"""
    fmt = probe.get("format", {})
    dur = fmt.get("duration")
    if dur:
        try:
            return float(dur)
        except (ValueError, TypeError):
            pass
    for stream in probe.get("streams", []):
        dur = stream.get("duration")
        if dur:
            try:
                return float(dur)
            except (ValueError, TypeError):
                pass
    return 0.0


def get_media_info(probe: dict[str, Any]) -> dict[str, Any]:
    """从 ffprobe JSON 中提取常用媒体信息。"""
    fmt = probe.get("format", {})
    info: dict[str, Any] = {
        "duration": _get_duration(probe),
        "size_bytes": int(fmt.get("size", 0) or 0),
        "format_name": fmt.get("format_name", ""),
        "has_audio": False,
        "has_video": False,
    }
    for stream in probe.get("streams", []):
        codec_type = stream.get("codec_type", "")
        if codec_type == "video":
            info["has_video"] = True
            info["width"] = int(stream.get("width", 0) or 0)
            info["height"] = int(stream.get("height", 0) or 0)
            info["video_codec"] = stream.get("codec_name", "")
        elif codec_type == "audio":
            info["has_audio"] = True
            info["audio_codec"] = stream.get("codec_name", "")
            info["sample_rate"] = int(stream.get("sample_rate", 0) or 0)
    return info


# ---------------------------------------------------------------------------
# faster-whisper 语音转文字
# ---------------------------------------------------------------------------

# 装的是 faster-whisper（CTranslate2 后端），不是 openai-whisper。
# openai-whisper 从来没进过 requirements.txt，于是 `import whisper` 恒 ImportError，
# analyze_voice 实测 0/9 成功、日志里 9 次 'whisper not installed'。
# 两者 API 不同：faster-whisper 的 transcribe() 返回 (segments 生成器, info)，
# 不迭代 segments 就不会真正解码，也没有 fp16 参数。

# 模型规格默认值：small 的权重已在本机 HF 缓存里，装载后转写 2~7s 的音频约 6~9s。
# 想换规格不要改这里 —— 走下面三个环境变量，或由调用方显式传参。
_ASR_DEFAULT_MODEL_SIZE = "small"
_ASR_DEFAULT_DEVICE = "cpu"
_ASR_DEFAULT_COMPUTE_TYPE = "int8"

# (size, device, compute_type) -> WhisperModel。
# 按规格做键而不是单个全局变量：规格变了必须换模型，否则改了配置还在用旧模型。
_whisper_models: dict[tuple[str, str, str], Any] = {}
_whisper_lock = asyncio.Lock()


def _resolve_asr_runtime(
    model_size: str | None,
    device: str | None,
    compute_type: str | None,
) -> tuple[str, str, str]:
    """定出本次用的模型规格：显式传参 > 环境变量 > 内置默认。

    utils/ 层拿不到 ConfigManager（它是 YukikoEngine 的实例属性），所以这里读
    环境变量 —— 也就是本仓配置三层里的第一层 .env。调用方能拿到配置时应显式传参。
    """
    resolved_size = str(
        model_size or os.getenv("YUKIKO_ASR_MODEL_SIZE", "") or _ASR_DEFAULT_MODEL_SIZE
    ).strip()
    resolved_device = str(
        device or os.getenv("YUKIKO_ASR_DEVICE", "") or _ASR_DEFAULT_DEVICE
    ).strip()
    resolved_compute = str(
        compute_type or os.getenv("YUKIKO_ASR_COMPUTE_TYPE", "") or _ASR_DEFAULT_COMPUTE_TYPE
    ).strip()
    return resolved_size, resolved_device, resolved_compute


def _to_simplified_transcript(text: str) -> str:
    """把转写结果转成简体。

    faster-whisper 对中文实测输出繁体（'吃米飯必須配西瓜'），本仓一律简体。
    复用 utils.text 里已有的 opencc t2s 转换器（opencc-python-reimplemented 已在
    requirements.txt），不另起一份转换器实例、更不手写字表。
    """
    content = str(text or "")
    if not content:
        return ""
    try:
        from utils.text import _to_simplified

        return _to_simplified(content)
    except Exception as exc:  # pragma: no cover - 转换器缺失时宁可返回原文
        _log.warning("asr_simplify_failed | reason=converter_unavailable | %s", exc)
        return content


# 复读机幻觉的判定门槛。都是结构性度量（长度 / 去重后占比），
# 不是"这句话听起来像幻觉"那种语义判断，也不是关键词表。
_HALLUCINATION_MIN_CHARS = 24
_HALLUCINATION_MAX_UNIQUE_RATIO = 0.34
_HALLUCINATION_NGRAM = 6
_HALLUCINATION_MIN_NGRAM_REPEAT = 4
_GUIDE_ECHO_MIN_OVERLAP_RATIO = 0.6


def _looks_like_repetition_hallucination(text: str) -> bool:
    """一段转写是不是"复读机"幻觉。

    Whisper 系模型在静音/低噪输入上会反复吐同一个短语。实测：
      6s 全零静音 -> '我只想要你和我一起去' × 11（120 字）
      6s 全零静音 -> '你不是说我会上课吗我会上课' + '我会上课' × 22（109 字）
    这种输出会以 ok=True + 【语音内容】 进群，比"听不了"糟得多。

    只用两个结构信号，不看内容：
      1. 去重字符占比过低（复读同一批字）
      2. 某个 6 字窗口重复出现 4 次以上
    真人说话即使有口头重复也很难同时满足这两条 —— 实测三条真实群语音
    去重占比分别约 0.87 / 1.00 / 0.79，都远高于门槛。
    """

    content = re.sub(r"\s+", "", str(text or ""))
    if len(content) < _HALLUCINATION_MIN_CHARS:
        return False
    if len(set(content)) / len(content) <= _HALLUCINATION_MAX_UNIQUE_RATIO:
        return True
    if len(content) >= _HALLUCINATION_NGRAM * _HALLUCINATION_MIN_NGRAM_REPEAT:
        counts: dict[str, int] = {}
        for i in range(len(content) - _HALLUCINATION_NGRAM + 1):
            gram = content[i : i + _HALLUCINATION_NGRAM]
            counts[gram] = counts.get(gram, 0) + 1
            if counts[gram] >= _HALLUCINATION_MIN_NGRAM_REPEAT:
                return True
    return False


def _transcript_echoes_guide(text: str, guide: str) -> bool:
    """转写结果是不是把 initial_prompt 引导词原文抄回来了。

    Pass-3 给模型喂了一句引导词（"这是一段日常群聊语音，包含二次元…"）。
    模型在没有真实语音时会把它当成内容吐出来，然后这段**仓里代码写的字**
    会以 data.text / display 的身份回喂给模型，被当成"用户说的话"发进群。
    判定按字符集重合比，不做字面相等 —— 模型会改标点、丢字、重复多遍。
    """

    body = re.sub(r"\s+", "", str(text or ""))
    hint = re.sub(r"\s+", "", str(guide or ""))
    if not body or len(hint) < 8:
        return False
    hint_chars = set(hint)
    overlap = sum(1 for ch in set(body) if ch in hint_chars)
    return overlap / max(1, len(set(body))) >= _GUIDE_ECHO_MIN_OVERLAP_RATIO


async def transcribe_audio_enhanced(
    audio_path: str | Path,
    *,
    model_size: str | None = None,
    device: str | None = None,
    compute_type: str | None = None,
    language: str | None = None,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """魔改版多轮语音识别系统（faster-whisper 后端）。

    采用多次解析和加权计分的机制：
    - Pass 1: 零温度带束搜索 (求稳)
    - Pass 2: 温度回退降级 + 无前置上下文干扰 (防幻觉)
    - Pass 3: 专属二次元/网络提词引导 (懂梗)
    最终按平均 logprob 和非语音概率计分，选取最优解，并自带分段排版。

    返回 dict：`text` / `score` / `pass` / `reason`，成功时另有 `formatted_text`
    与 `raw_segments`。`pass="engine_missing"` 专指引擎侧不可用（没装、权重装不上），
    调用方据此区分「环境没装好」和「音频里真没人说话」，别改这个取值。
    """
    audio_path = Path(audio_path)
    if not audio_path.is_file():
        _log.warning("asr_failed | reason=audio_file_missing | file=%s", audio_path.name)
        return {"text": "", "score": -999, "pass": "none", "reason": "audio_file_missing"}

    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:
        # pass 必须能把「引擎不在」和「音频里真没人说话」分开：以前两者都是
        # pass="error" + 空 text，调用方只看 text，于是把没装引擎说成了「静音」。
        _log.warning("asr_failed | reason=asr_engine_missing | detail=%s", exc)
        return {
            "text": "",
            "score": -999,
            "pass": "engine_missing",
            "reason": "asr_engine_missing",
        }

    resolved_size, resolved_device, resolved_compute = _resolve_asr_runtime(
        model_size, device, compute_type
    )
    cache_key = (resolved_size, resolved_device, resolved_compute)
    loop = asyncio.get_running_loop()

    # 装载很贵（首次要下权重，实测 386.8s），所以进程内按规格单例复用，
    # 且放 executor 里免得阻塞事件循环。锁保证并发首调只装一次。
    async with _whisper_lock:
        if cache_key not in _whisper_models:
            _log.info(
                "asr_loading_model | size=%s | device=%s | compute=%s",
                resolved_size,
                resolved_device,
                resolved_compute,
            )
            try:
                _whisper_models[cache_key] = await loop.run_in_executor(
                    None,
                    lambda: WhisperModel(
                        resolved_size,
                        device=resolved_device,
                        compute_type=resolved_compute,
                    ),
                )
            except Exception as exc:
                # 权重下载失败、磁盘满、缓存损坏、compute_type 该设备不支持都走这里。
                # 换一份音频回来还是装不上，所以归到 engine_missing 让调用方别再试。
                _log.warning(
                    "asr_failed | reason=asr_model_load_failed | size=%s | device=%s"
                    " | compute=%s | exc=%s | detail=%s",
                    resolved_size,
                    resolved_device,
                    resolved_compute,
                    type(exc).__name__,
                    exc,
                )
                return {
                    "text": "",
                    "score": -999,
                    "pass": "engine_missing",
                    "reason": "asr_model_load_failed",
                }
            _log.info("asr_model_loaded | size=%s | device=%s", resolved_size, resolved_device)

    model = _whisper_models[cache_key]

    def _score_result(res: dict[str, Any]) -> float:
        segments = res.get("segments", [])
        if not segments:
            return -999.0
        avg_logprob = sum(s.get("avg_logprob", -1.0) for s in segments) / len(segments)
        avg_no_speech = sum(s.get("no_speech_prob", 1.0) for s in segments) / len(segments)
        
        # 分数计算公式 (logprob 越接近0越好，no_speech越小越好)
        return float(avg_logprob * 0.7 - avg_no_speech * 0.3)

    def _format_segments(res: dict[str, Any]) -> str:
        texts = []
        for s in res.get("segments", []):
            start = f"{s.get('start', 0):.1f}s"
            end = f"{s.get('end', 0):.1f}s"
            txt = s.get("text", "").strip()
            if txt:
                texts.append(f"[{start} - {end}] {txt}")
        if not texts:
            return res.get("text", "").strip()
        return "\n".join(texts)

    def _collect(pass_name: str, **kwargs: Any) -> dict[str, Any]:
        """跑一趟 transcribe 并把生成器抽干，归一成旧的 dict 形状。

        faster-whisper 的 segments 是懒生成器 —— 不 for 一遍就没真解码，异常也要到
        迭代时才抛，所以抽干必须在这个 try 里面。
        """
        # vad_filter 必须每趟都开。不开的话静音/低噪会被幻觉成整段中文，
        # 而且以 ok=True + 【语音内容】 进群 —— 比"听不了"糟得多。
        # 实测（6s 全零静音，同一份文件跑两次）：
        #   run0 -> '你不是说我会上课吗我会上课我会上课…'（109 字）
        #   run1 -> '我只想要你和我一起去…'（120 字，重复 11 遍）
        # 6s 低噪 -> '请不吝点赞、订阅、转发、打赏…'（Whisper 训练数据污染的经典形态）
        # VAD 先把非语音段切掉，没有语音段就没有 segments，text 自然为空，
        # 于是能正确落到调用方的「真静音」分支。
        call_kwargs = {
            "vad_filter": True,
            "vad_parameters": {"min_silence_duration_ms": 500},
            **kwargs,
        }
        segments_iter, info = model.transcribe(
            str(audio_path), language=language, **call_kwargs
        )
        segments: list[dict[str, Any]] = []
        for seg in segments_iter:
            segments.append(
                {
                    "start": float(getattr(seg, "start", 0.0) or 0.0),
                    "end": float(getattr(seg, "end", 0.0) or 0.0),
                    "text": _to_simplified_transcript(getattr(seg, "text", "") or ""),
                    "avg_logprob": float(getattr(seg, "avg_logprob", -1.0) or -1.0),
                    "no_speech_prob": float(getattr(seg, "no_speech_prob", 1.0) or 1.0),
                }
            )
        joined = "".join(s["text"] for s in segments)
        guide = str(call_kwargs.get("initial_prompt", "") or "").strip()
        if guide and _transcript_echoes_guide(joined, guide):
            # Pass-3 的引导词被模型原文抄进了结果。那段字是仓里代码写的，
            # 不是任何人说的话；它会以 data.text / display 的身份回喂给模型，
            # 然后被当成"用户说的"发进群 —— 正是要消灭的那类内部状态泄漏。
            # 任何 prompt 层禁令都挡不住它（它走的是工具输出，不是模型自由生成）。
            _log.warning(
                "asr_guide_echo_dropped | pass=%s | chars=%d",
                pass_name,
                len(joined),
            )
            segments = []
            joined = ""
        elif joined and _looks_like_repetition_hallucination(joined):
            _log.warning(
                "asr_repetition_hallucination_dropped | pass=%s | chars=%d | head=%s",
                pass_name,
                len(joined),
                joined[:40],
            )
            segments = []
            joined = ""
        res: dict[str, Any] = {
            "text": joined,
            "segments": segments,
            "language": str(getattr(info, "language", "") or ""),
            "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
            "duration": float(getattr(info, "duration", 0.0) or 0.0),
            "_pass": pass_name,
        }
        res["_score"] = _score_result(res)
        return res

    def _do_transcribes() -> list[dict[str, Any]]:
        results = []

        # =======================================================
        # Pass 1: 零温度带束搜索 (标准最优路径)
        # =======================================================
        try:
            results.append(_collect("Pass-1-BeamSearch", temperature=0.0, beam_size=5))
        except Exception as exc:
            _log.warning(
                "asr_pass_failed | pass=Pass-1-BeamSearch | exc=%s | detail=%s",
                type(exc).__name__,
                exc,
            )

        # 优化短路：如果第一次效果极好，直接返回不跑后面的了，省点算力
        if results and results[0].get("_score", -999.0) > -0.3:
            return results

        # =======================================================
        # Pass 2: 防止幻觉和复读机的回退模式
        # =======================================================
        try:
            results.append(
                _collect(
                    "Pass-2-NoContext",
                    temperature=(0.2, 0.4, 0.6),  # 允许自动回退寻找稳定态
                    condition_on_previous_text=False,  # 切断上下文关联，防止复读机幻觉
                )
            )
        except Exception as exc:
            _log.warning(
                "asr_pass_failed | pass=Pass-2-NoContext | exc=%s | detail=%s",
                type(exc).__name__,
                exc,
            )

        # =======================================================
        # Pass 3: 二次元/日常梗的 Prompt 增强引导
        # =======================================================
        try:
            results.append(
                _collect(
                    "Pass-3-PromptGuided",
                    temperature=0.0,
                    initial_prompt="这是一段日常群聊语音，包含二次元、原神、游戏、技术梗等网络常用语。",
                )
            )
        except Exception as exc:
            _log.warning(
                "asr_pass_failed | pass=Pass-3-PromptGuided | exc=%s | detail=%s",
                type(exc).__name__,
                exc,
            )

        return results

    try:
        all_results = await asyncio.wait_for(
            loop.run_in_executor(None, _do_transcribes),
            timeout=timeout,
        )
        if not all_results:
            _log.warning(
                "asr_failed | reason=transcribe_all_passes_failed | file=%s", audio_path.name
            )
            return {
                "text": "",
                "score": -999,
                "pass": "error",
                "reason": "transcribe_all_passes_failed",
            }

        # 排序：分数从高到低
        all_results.sort(key=lambda x: x.get("_score", -999.0), reverse=True)
        best = all_results[0]

        best_text = best.get("text", "").strip()
        formatted_text = _format_segments(best)
        score = best.get("_score", -999.0)
        pass_name = best.get("_pass", "unknown")

        _log.info(
            "asr_transcribed | file=%s | chars=%d | best_pass=%s | score=%.2f | lang=%s"
            " | audio_seconds=%.1f",
            audio_path.name,
            len(best_text),
            pass_name,
            score,
            best.get("language", "") or "-",
            best.get("duration", 0.0) or 0.0,
        )
        if not best_text:
            # 转成功但一个字都没有 —— 调用方会把它归到 voice_transcribe_empty
            # （「真静音」），和引擎不在是两回事，所以这条也要能在日志里看出来。
            _log.warning(
                "asr_empty_transcript | file=%s | best_pass=%s | audio_seconds=%.1f",
                audio_path.name,
                pass_name,
                best.get("duration", 0.0) or 0.0,
            )

        return {
            "text": best_text,
            "formatted_text": formatted_text,
            "score": score,
            "pass": pass_name,
            "reason": "",
            "raw_segments": best.get("segments", []),
        }

    except asyncio.TimeoutError:
        _log.warning(
            "asr_failed | reason=transcribe_timeout | file=%s | timeout=%.1f",
            audio_path.name,
            timeout,
        )
        return {"text": "", "score": -999, "pass": "timeout", "reason": "transcribe_timeout"}
    except Exception as exc:
        _log.warning(
            "asr_failed | reason=transcribe_exception | file=%s | exc=%s | detail=%s",
            audio_path.name,
            type(exc).__name__,
            exc,
        )
        return {"text": "", "score": -999, "pass": "error", "reason": "transcribe_exception"}


async def transcribe_audio(
    audio_path: str | Path,
    *,
    model_size: str | None = None,
    language: str | None = None,
    timeout: float = 120.0,
) -> str:
    """（向后兼容层）用本地 faster-whisper 模型转录音频为文字。"""
    res = await transcribe_audio_enhanced(
        audio_path, model_size=model_size, language=language, timeout=timeout
    )
    return res.get("text", "")


# ---------------------------------------------------------------------------
# 通用下载
# ---------------------------------------------------------------------------


async def download_file(
    url: str,
    output_path: str | Path,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    max_size_mb: float = 100.0,
) -> bool:
    """异步下载文件到指定路径。"""
    import httpx

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = int(max_size_mb * 1024 * 1024)

    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, verify=True
        ) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                total = 0
                with open(output_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(8192):
                        total += len(chunk)
                        if total > max_bytes:
                            _log.warning("download_size_exceeded | url=%s | max=%sMB", url[:80], max_size_mb)
                            output_path.unlink(missing_ok=True)
                            return False
                        f.write(chunk)
        return output_path.is_file() and output_path.stat().st_size > 0
    except Exception as exc:
        _log.warning("download_failed | url=%s | %s", url[:80], exc)
        output_path.unlink(missing_ok=True)
        return False


def file_hash(path: str | Path, algo: str = "md5") -> str:
    """计算文件哈希。"""
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_filename(name: str, max_len: int = 80) -> str:
    """将任意字符串转为安全文件名。"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"_+", "_", name).strip("_. ")
    return name[:max_len] if name else "unnamed"
