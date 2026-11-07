# -*- coding: utf-8 -*-
"""astrbot_plugin_music_request

群里点歌，免费听。

链路::

    点歌 周杰伦 晴天
      -> AI 解析意图（歌手/歌名，可选）
      -> 网易云搜索
      -> 按匹配度排序
      -> 逐个探测官方外链能否播放（版权歌会 404）
      -> 发语音消息，失败降级为音频直链

版权说明：只用网易云对外开放的分享外链，不登录、不破解。
VIP 独占或版权下架的歌曲拿不到，插件会自动挑能播的版本。
"""

from __future__ import annotations

import asyncio
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
import asyncio
import os
import shutil
import tempfile
from typing import Any, AsyncGenerator

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star

try:  # 旧版本可能没有导出 AstrBotConfig
    from astrbot.api import AstrBotConfig
except Exception:  # pragma: no cover
    AstrBotConfig = dict  # type: ignore

# 发语音需要 Record 组件
try:
    from astrbot.api.message_components import Plain, Record

    HAS_RECORD = True
except Exception:  # pragma: no cover
    try:
        from astrbot.core.message.components import Plain, Record

        HAS_RECORD = True
    except Exception:
        Record = None  # type: ignore
        HAS_RECORD = False

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from music_core import (  # noqa: E402
    Song,
    build_caption,
    build_keyword,
    format_duration,
    is_instrumental,
    parse_intent_json,
    rank_songs,
    sanitize_recommendation,
    source_label,
    strip_command_prefix,
    wants_instrumental,
)
from music_source import build_library  # noqa: E402
from netease import MusicError  # noqa: E402

PLUGIN_NAME = "astrbot_plugin_music_request"

#: 没有指定歌曲时的默认点播词
DEFAULT_KEYWORDS = [
    "周杰伦 晴天",
    "告白气球",
    "起风了",
    "平凡之路",
    "光年之外",
]

INTENT_PROMPT = (
    "你是点歌助手。从用户这句话里提取点歌信息，只输出 JSON，不要任何解释。\n"
    '格式：{"keyword":"用于搜索的关键词","artist":"歌手","title":"歌名"}\n'
    "没有的字段留空字符串。\n"
    "用户：{text}"
)

RECOMMEND_PROMPT = (
    "用户想听的歌没搜到，请推荐一首真实存在的中文歌曲。\n"
    "只输出「歌名 歌手」，不要解释、不要引号、不要列表。\n"
    "用户想听：{text}"
)

#: 清理消息开头的 @昵称
AT_PREFIX_RE = re.compile(r"^@\S+\s*")

#: 带前缀的点歌（/点歌 xxx）。
#: wake_prefix 为空时 @filter.command 只认不带前缀的写法。
PREFIXED_MUSIC_RE = re.compile(r"^[/!！]\s*(?:点歌|来首歌|听歌|来首|想听|放首歌|放首)")

#: 限流缓存上限
CACHE_MAX_SIZE = 500


@dataclass
class ChoiceSession:
    """一次"等待用户选序号"的点歌会话。"""

    keyword: str
    songs: list[Song]
    expire_at: float


class MusicRequestPlugin(Star):
    """点歌插件。"""

    def _ensure_ffmpeg(self) -> None:
        """把可用的 ffmpeg 注入进程 PATH。

        AstrBot 转 silk 时用 ``shutil.which("ffmpeg")``，如果进程启动时 PATH
        里没有 ffmpeg，后面再装也不会被看到。这里在插件加载时补进去。
        """
        if shutil.which("ffmpeg"):
            return

        try:
            import imageio_ffmpeg

            source = Path(imageio_ffmpeg.get_ffmpeg_exe())
            if not source.exists():
                return

            target_dir = Path(tempfile.gettempdir()) / "astrbot_ffmpeg"
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / "ffmpeg.exe"
            if not target.exists() or target.stat().st_size != source.stat().st_size:
                shutil.copy2(source, target)

            os.environ["PATH"] = (
                str(target_dir) + os.pathsep + os.environ.get("PATH", "")
            )
            logger.info(f"[music] 已注入 ffmpeg 路径：{target}")
        except Exception as exc:
            logger.debug(f"[music] 注入 ffmpeg 失败：{exc}")

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)

        self._ensure_ffmpeg()

        self.config = config if config is not None else {}

        self.enable = self._cfg_bool("enable", True)
        self.use_ai = self._cfg_bool("use_ai", True)
        self.llm_provider_id = str(self._cfg("llm_provider_id", "") or "").strip()
        self.default_keywords = self._cfg_list("default_keywords") or list(
            DEFAULT_KEYWORDS
        )
        self.search_limit = self._cfg_int("search_limit", 20, minimum=5, maximum=30)
        self.probe_limit = self._cfg_int("probe_limit", 5, minimum=1, maximum=8)
        self.record_max_seconds = self._cfg_int(
            "record_max_seconds", 60, minimum=10, maximum=300
        )
        self.record_max_parts = self._cfg_int(
            "record_max_parts", 6, minimum=1, maximum=15
        )
        self.send_mode = str(self._cfg("send_mode", "record") or "record").strip().lower()
        if self.send_mode not in ("card", "record", "text"):
            self.send_mode = "card"
        self.rate_limit_seconds = self._cfg_int(
            "rate_limit_seconds", 5, minimum=0, maximum=120
        )
        self.enable_choice = self._cfg_bool("enable_choice", True)
        self.choice_count = self._cfg_int("choice_count", 10, minimum=2, maximum=15)
        self.choice_timeout = self._cfg_int(
            "choice_timeout", 120, minimum=30, maximum=600
        )

        self.source_names = self._cfg_list("sources") or ["netease", "qq"]
        self.client = build_library(
            self.source_names,
            timeout=self._cfg_int("request_timeout", 10, minimum=3, maximum=60),
        )
        self._last_request: dict[str, float] = {}
        self._choices: dict[str, ChoiceSession] = {}
        self._rng = random.Random()

        logger.info(
            "[music] 已加载 | 启用=%s | 音源=%s | AI=%s | 发送方式=%s | 语音组件=%s",
            self.enable,
            self.client.labels,
            self.use_ai,
            self.send_mode,
            HAS_RECORD,
        )

    # ------------------------------------------------------------------ 配置

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "开")
        return bool(value)

    def _cfg_int(
        self, key: str, default: int, minimum: int = 0, maximum: int = 10**6
    ) -> int:
        try:
            value = int(self._cfg(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    def _cfg_list(self, key: str) -> list[str]:
        value = self._cfg(key, [])
        if isinstance(value, str):
            return [
                item.strip() for item in re.split(r"[,，\n]+", value) if item.strip()
            ]
        if not isinstance(value, (list, tuple, set)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    # ------------------------------------------------------------------ 命令

    @filter.command("点歌")
    async def cmd_request(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """点歌：/点歌 周杰伦 晴天"""
        async for result in self._handle_music(event):
            yield result

    @filter.command("来首歌")
    async def cmd_play(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """来首歌：/来首歌 起风了"""
        async for result in self._handle_music(event):
            yield result

    @filter.command("听歌")
    async def cmd_listen(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """听歌：/听歌 稻香"""
        async for result in self._handle_music(event):
            yield result

    # ------------------------------------------------------------------ 主流程

    async def _handle_music(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        if not self.enable:
            return

        sender_key = self._sender_key(event)
        if self._is_flooding(sender_key):
            yield event.plain_result("点歌有点频繁，缓几秒再点。")
            return

        raw_text = self._clean_text(event)
        raw_text = strip_command_prefix(raw_text)
        umo = str(getattr(event, "unified_msg_origin", "") or "")

        intent = await self._parse_intent(raw_text, umo)
        keyword = build_keyword(intent, raw_text)

        # 没说要听什么，随机点一首
        if not keyword:
            keyword = self._rng.choice(self.default_keywords)
            raw_text = keyword

        try:
            songs = await self.client.search(keyword, limit=self.search_limit)
        except MusicError as exc:
            logger.warning(f"[music] 搜索失败：{exc}")
            yield event.plain_result("音乐服务暂时不可用，稍后再试。")
            return
        except Exception as exc:  # pragma: no cover
            logger.exception(f"[music] 搜索异常：{exc}")
            yield event.plain_result("音乐服务暂时不可用，稍后再试。")
            return

        # 搜不到时让模型推荐一首真实存在的歌再试
        if not songs and self.use_ai and raw_text:
            alternative = await self._recommend(raw_text, umo)
            if alternative:
                try:
                    songs = await self.client.search(
                        alternative, limit=self.search_limit
                    )
                except MusicError:
                    songs = []
                if songs:
                    keyword = alternative
                    logger.info(f"[music] 改用 AI 推荐：{alternative}")

        if not songs:
            yield event.plain_result(f"没找到「{keyword}」，换个歌名或带上歌手名试试。")
            return

        ranked = rank_songs(
            songs,
            artist=intent.get("artist", ""),
            title=intent.get("title", ""),
        )
        prefer_instrumental = wants_instrumental(raw_text)

        # 开启选择时先列候选，让用户回复序号挑第几首
        if self.enable_choice and len(ranked) > 1:
            candidates = ranked[: self.choice_count]
            self._remember_choices(sender_key, keyword, candidates)
            yield event.plain_result(self._format_choices(keyword, candidates))
            event.stop_event()
            return

        playable = await self._find_playable(
            ranked, prefer_instrumental=prefer_instrumental
        )

        if playable is None:
            yield event.plain_result(
                "找到这首歌了，但暂时播放不了（多半是版权或 VIP 限制）。\n"
                "换一首试试，或者带上「翻唱」两个字再点一次。"
            )
            return

        song, audio_url = playable
        logger.info(f"[music] 命中 {song.display} ({song.id})")
        async for result in self._deliver(event, song, audio_url, prefer_instrumental):
            yield result

    # ------------------------------------------------------------------ 序号选择

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_choice(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """全消息入口：优先兼容带前缀的点歌，其次处理序号选择。"""
        try:
            # wake_prefix 为空时 /点歌 匹配不到命令，这里兜一层
            if PREFIXED_MUSIC_RE.match(self._clean_text(event)):
                async for result in self._handle_music(event):
                    yield result
                return

            async for result in self._handle_choice(event):
                yield result
        except Exception as exc:  # pragma: no cover
            logger.exception(f"[music] 处理消息时出现未捕获异常：{exc}")

    async def _handle_choice(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        key = self._sender_key(event)
        session = self._choices.get(key)

        if session is None:
            return
        if session.expire_at < time.monotonic():
            self._choices.pop(key, None)
            return

        text = self._clean_text(event)
        if not re.fullmatch(r"\d{1,2}", text or ""):
            return

        index = int(text) - 1
        total = len(session.songs)
        if index < 0 or index >= total:
            event.stop_event()
            yield event.plain_result(f"序号超出范围，回复 1-{total} 之间的数字。")
            return

        song = session.songs[index]
        event.stop_event()

        try:
            audio_url = await self.client.probe(song)
        except MusicError as exc:
            logger.warning(f"[music] 探测失败：{exc}")
            audio_url = None

        if not audio_url:
            # 保留会话，让用户还能换个序号
            yield event.plain_result(
                f"「{song.display}」播不了（版权或 VIP 限制），换一个序号试试。"
            )
            return

        self._choices.pop(key, None)
        logger.info(f"[music] 用户选择 {song.display} ({song.id})")
        async for result in self._deliver(
            event, song, audio_url, wants_instrumental(session.keyword)
        ):
            yield result

    def _remember_choices(self, key: str, keyword: str, songs: list[Song]) -> None:
        now = time.monotonic()
        if len(self._choices) > CACHE_MAX_SIZE:
            self._choices = {
                k: v for k, v in self._choices.items() if v.expire_at > now
            }
        self._choices[key] = ChoiceSession(
            keyword=keyword,
            songs=songs,
            expire_at=now + self.choice_timeout,
        )

    def _format_choices(self, keyword: str, songs: list[Song]) -> str:
        # 群里刷屏太厉害，候选列表尽量压短：去掉时长和平台全名
        short_source = {"netease": "网", "qq": "Q", "kuwo": "酷", "audius": "A"}
        multi_source = len(getattr(self.client, "order", [])) > 1

        lines = [f"「{keyword}」{len(songs)} 个版本，回复序号："]
        for index, song in enumerate(songs, 1):
            line = f"{index}.{song.name}-{song.artist_text}"
            if multi_source:
                tag = short_source.get(song.source, "")
                if tag:
                    line += f"·{tag}"
            lines.append(line)
        lines.append(f"{self.choice_timeout} 秒内回复数字")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 发送

    async def _deliver(
        self,
        event: AstrMessageEvent,
        song: Song,
        audio_url: str,
        prefer_instrumental: bool = False,
    ) -> AsyncGenerator[MessageEventResult, None]:
        """按配置的发送方式把歌发出去。"""
        caption = build_caption(song, event.get_sender_name())
        if is_instrumental(song) and not prefer_instrumental:
            caption += "\n（原版暂时不可播，给你找了伴奏版）"

        if self.send_mode == "text":
            yield event.plain_result(f"{caption}\n{audio_url}")
            return

        if self.send_mode == "card":
            if await self._send_music_card(event, song, audio_url):
                return
            # 卡片发不出去就退回文本+直链，不能让用户什么都收不到
            yield event.plain_result(f"{caption}\n{audio_url}")
            return

        # record 模式：按每段 N 秒切分，逐条发送，这样整首都能听到
        if HAS_RECORD:
            parts = await self._prepare_voice_parts(audio_url)
            logger.info(f"[music] 语音分段：{len(parts)} 段")
            if parts:
                asyncio.create_task(self._cleanup_later(parts))
                total = len(parts)
                sent = 0
                for index, part in enumerate(parts, 1):
                    label = f"{caption}（{index}/{total}）" if total > 1 else caption
                    try:
                        await event.send(
                            event.chain_result([Plain(label), Record(file=part)])
                        )
                        sent += 1
                    except Exception as exc:
                        logger.warning(f"[music] 第 {index} 段语音发送失败：{exc}")
                        break
                    if index < total:
                        await asyncio.sleep(1)
                if sent:
                    return

        yield event.plain_result(f"音频直链：{audio_url}")

    async def _cleanup_later(self, paths, delay: int = 300) -> None:
        """延后删除临时语音文件，等平台读完再清理。"""
        await asyncio.sleep(delay)
        if isinstance(paths, str):
            paths = [paths]
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass

    async def _prepare_voice_parts(self, audio_url: str) -> list[str]:
        """把整首按每段 N 秒切分，返回分段文件列表。

        没有 ffmpeg 或下载失败时返回空列表，上层会退回直接发原始链接。
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            # 没装系统 ffmpeg 时，退回 imageio-ffmpeg 自带的二进制
            try:
                import imageio_ffmpeg

                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                ffmpeg = None
        if not ffmpeg:
            logger.warning("[music] 未找到 ffmpeg，无法转语音")
            return []

        try:
            async with httpx.AsyncClient(timeout=40, follow_redirects=True) as client:
                response = await client.get(audio_url)
                response.raise_for_status()
        except Exception as exc:
            logger.warning(f"[music] 下载音频失败：{exc}")
            return []

        if len(response.content) < 2048:
            return []

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
            handle.write(response.content)
            source_path = handle.name

        out_dir = tempfile.mkdtemp(prefix="music_parts_")
        pattern = os.path.join(out_dir, "part_%03d.mp3")
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-y",
                "-i",
                source_path,
                "-f",
                "segment",
                "-segment_time",
                str(self.record_max_seconds),
                "-reset_timestamps",
                "1",
                "-c",
                "copy",
                pattern,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()
        except Exception as exc:
            logger.warning(f"[music] 切分失败：{exc}")
        finally:
            try:
                os.unlink(source_path)
            except OSError:
                pass

        if process is None or process.returncode != 0:
            return []

        parts = [
            str(item)
            for item in sorted(Path(out_dir).glob("part_*.mp3"))
            if item.stat().st_size > 1024
        ]
        if not parts:
            return []
        return parts[: max(1, self.record_max_parts)]

    async def _send_music_card(
        self, event: AstrMessageEvent, song: Song, audio_url: str
    ) -> bool:
        """直接调用协议端 API 发自定义音乐卡片。

        AstrBot 的 ``Music`` 组件把 ``_type`` 当私有属性，序列化后没有 ``type`` 字段，
        NapCat 收到会报「消息体无法解析」。而 NapCat 又不支持 ``type=163 + id`` 的简写卡片，
        只能用 ``custom`` 并补齐 url/audio/title/content/image。
        所以这里绕过组件层，直接发标准 OneBot music 段。
        """
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return False
        if not song.cover:
            logger.debug("[music] 没有封面图，跳过卡片改为文本")
            return False

        segment = {
            "type": "music",
            "data": {
                "type": "custom",
                "url": f"https://music.163.com/song?id={song.id}",
                "audio": audio_url,
                "title": song.name,
                "content": song.artist_text,
                "image": song.cover,
            },
        }

        try:
            group_id = str(event.get_group_id() or "")
        except Exception:
            group_id = ""

        try:
            if group_id:
                await bot.call_action(
                    "send_group_msg",
                    group_id=int(group_id),
                    message=[segment],
                )
            else:
                await bot.call_action(
                    "send_private_msg",
                    user_id=int(event.get_sender_id()),
                    message=[segment],
                )
            return True
        except Exception as exc:
            logger.warning(f"[music] 音乐卡片发送失败，降级为文本：{exc}")
            return False

    # ------------------------------------------------------------------ 解析与探测

    async def _find_playable(
        self, ranked: list[Song], prefer_instrumental: bool = False
    ) -> tuple[Song, str] | None:
        """先找有人声的版本，都不可播再退到伴奏。

        用户点的是歌，不是伴奏。但有些歌只有伴奏能播，所以伴奏留作兜底，
        不能直接丢掉。
        """
        if prefer_instrumental:
            # 用户明确要伴奏，伴奏组先试
            vocal = [song for song in ranked if not is_instrumental(song)]
            backup = [song for song in ranked if is_instrumental(song)]
            groups = [backup, vocal]
        else:
            vocal = [song for song in ranked if not is_instrumental(song)]
            backup = [song for song in ranked if is_instrumental(song)]
            groups = [vocal, backup]

        for index, group in enumerate(groups):
            # 伴奏作为兜底时只试前两首，避免探测太多拖慢响应
            limit = self.probe_limit if index == 0 else min(2, self.probe_limit)
            candidates = group[:limit]
            if not candidates:
                continue

            results = await asyncio.gather(
                *(self.client.probe(song) for song in candidates),
                return_exceptions=True,
            )

            for song, result in zip(candidates, results):
                if isinstance(result, str) and result:
                    return song, result
                if isinstance(result, Exception):
                    logger.debug(f"[music] 探测 {song.id} 失败：{result}")
        return None

    async def _parse_intent(self, text: str, umo: str = "") -> dict[str, str]:
        """让模型把「来首周杰伦那种伤感情歌」拆成歌手/歌名。失败就用原文搜索。"""
        fallback = {"keyword": text} if text else {}
        if not self.use_ai or not text:
            return fallback

        # 模板里有 JSON 示例的花括号，不能用 str.format
        prompt = INTENT_PROMPT.replace("{text}", text[:120])
        reply = await self._llm(prompt, umo)
        intent = parse_intent_json(reply)
        if not intent:
            return fallback

        if not build_keyword(intent):
            return fallback
        return intent

    async def _recommend(self, text: str, umo: str = "") -> str:
        if not self.use_ai:
            return ""
        prompt = RECOMMEND_PROMPT.replace("{text}", text[:120])
        reply = await self._llm(prompt, umo)
        return sanitize_recommendation(reply)

    async def _llm(self, prompt: str, umo: str = "") -> str:
        try:
            provider_id = self.llm_provider_id
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not provider_id:
                return ""
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            return str(getattr(response, "completion_text", "") or "").strip()
        except asyncio.TimeoutError:
            logger.warning("[music] AI 调用超时")
            return ""
        except Exception as exc:
            logger.warning(f"[music] AI 调用失败：{exc}")
            return ""

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _clean_text(event: AstrMessageEvent) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        text = AT_PREFIX_RE.sub("", text)
        return text.strip()

    @staticmethod
    def _sender_key(event: AstrMessageEvent) -> str:
        try:
            origin = str(event.unified_msg_origin or "")
        except Exception:
            origin = ""
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:
            sender = ""
        return f"{origin}|{sender}"

    def _is_flooding(self, key: str) -> bool:
        if self.rate_limit_seconds <= 0:
            return False
        now = time.monotonic()
        last = self._last_request.get(key, 0.0)
        if now - last < self.rate_limit_seconds:
            return True
        if len(self._last_request) > CACHE_MAX_SIZE:
            self._last_request.clear()
        self._last_request[key] = now
        return False

    # ------------------------------------------------------------------ 生命周期

    async def terminate(self) -> None:
        self._last_request.clear()
        self._choices.clear()
        try:
            await self.client.close()
        except Exception as exc:
            logger.debug(f"[music] 关闭客户端时出错：{exc}")
        logger.info("[music] 插件已卸载")
