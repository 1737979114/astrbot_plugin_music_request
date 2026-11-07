# -*- coding: utf-8 -*-
"""点歌的纯逻辑层：歌曲打分、搜索词构建、AI 返回解析、时长格式化。

只依赖标准库，方便单独测试。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable

#: 歌名里常见的版本后缀，匹配时先去掉，避免「晴天 (Live)」匹配不上「晴天」
VERSION_SUFFIX_RE = re.compile(
    r"\s*[\(（\[【](?:live|remix|伴奏|纯音乐|翻唱|女声|男声|深情|钢琴|吉他|dj|版|cover)[^\)）\]】]*[\)）\]】]",
    re.IGNORECASE,
)

#: 命令词，解析点歌内容时要剥掉
COMMAND_PREFIX_RE = re.compile(
    r"^[/!！]?\s*(?:点歌|来首歌|来首|听歌|想听|放首歌|放首)\s*[:：]?\s*",
)

#: 从模型输出里抠 JSON
JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)

#: 伴奏/纯音乐版本。用户点的是歌，不能给伴奏。
INSTRUMENTAL_RE = re.compile(
    r"(伴奏|纯音乐|无人声|instrumental|off\s*vocal|karaoke)",
    re.IGNORECASE,
)


@dataclass
class Song:
    """一首搜索结果。"""

    id: str
    name: str
    artists: list[str] = field(default_factory=list)
    album: str = ""
    duration_ms: int = 0
    fee: int = 0
    #: 封面图，发自定义音乐卡片时是必填项
    cover: str = ""
    #: 来源：netease / qq，探测播放地址时按它分发
    source: str = "netease"

    @property
    def artist_text(self) -> str:
        return "、".join(self.artists) if self.artists else "未知歌手"

    @property
    def display(self) -> str:
        return f"{self.name} - {self.artist_text}"


def strip_command_prefix(text: str) -> str:
    """把「点歌 xxx」里的命令词剥掉，只留下搜索内容。"""
    return COMMAND_PREFIX_RE.sub("", text or "").strip()


def clean_title(name: str) -> str:
    """去掉歌名里的版本后缀和多余空格，用于匹配。"""
    value = VERSION_SUFFIX_RE.sub("", name or "")
    return re.sub(r"\s+", " ", value).strip().lower()


def is_instrumental(song: Song) -> bool:
    """是否是伴奏/纯音乐版本。"""
    return bool(INSTRUMENTAL_RE.search(song.name or ""))


def wants_instrumental(text: str) -> bool:
    """用户是不是明确要伴奏/纯音乐。"""
    return bool(INSTRUMENTAL_RE.search(text or ""))


def format_duration(duration_ms: int) -> str:
    """毫秒转 ``m:ss``。"""
    if not duration_ms or duration_ms <= 0:
        return "--:--"
    total_seconds = int(duration_ms) // 1000
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


def score_song(song: Song, artist: str = "", title: str = "") -> int:
    """给搜索结果打分，分越高越可能是用户想要的那首。

    版权歌经常搜不到原唱，只能退而求其次，所以这里只是排序，
    最终能不能播还要靠 :func:`probe` 逐个验证。
    """
    score = 0
    clean_name = clean_title(song.name)
    clean_want = clean_title(title)

    if clean_want:
        if clean_name == clean_want:
            score += 50
        elif clean_want in clean_name or clean_name in clean_want:
            score += 30
        # 标题本身干净的（没有 Live/翻唱/片段后缀）更接近用户预期
        if song.name.strip().lower() == clean_want:
            score += 10

    if artist:
        want_artist = artist.strip().lower()
        artist_names = [item.strip().lower() for item in song.artists]
        if any(name == want_artist for name in artist_names):
            score += 45
        elif any(want_artist in name or name in want_artist for name in artist_names):
            score += 25

    # 免费/无版权限制的优先
    if song.fee == 0:
        score += 12

    # 时长：优先完整歌曲。实测搜索里混着 1 分多钟的剪辑版/片段，
    # 不加权会出现"点《晴天》放了 1 分 52 秒"的情况。
    if song.duration_ms:
        score += 1
        if 120_000 <= song.duration_ms <= 420_000:
            score += 15
        elif song.duration_ms < 90_000:
            score -= 20

    # 伴奏/纯音乐排到后面：用户点的是歌，不是伴奏。
    # 不能直接过滤，因为有些歌只有伴奏能播，需要留作兜底。
    if is_instrumental(song):
        score -= 60

    return score


def rank_songs(
    songs: Iterable[Song],
    artist: str = "",
    title: str = "",
) -> list[Song]:
    """按匹配度降序排列。"""
    items = list(songs)
    return sorted(items, key=lambda song: score_song(song, artist, title), reverse=True)


def parse_intent_json(text: str) -> dict[str, str]:
    """解析模型返回的 JSON，容忍 ```json 包裹和多余解释。"""
    if not text:
        return {}

    match = JSON_BLOCK_RE.search(text)
    if not match:
        return {}

    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return {}

    if not isinstance(data, dict):
        return {}

    result: dict[str, str] = {}
    for key in ("keyword", "artist", "title", "mood"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()
    return result


def build_keyword(intent: dict[str, str], fallback: str = "") -> str:
    """把意图拼成搜索词。

    优先用模型给的 keyword；没有就拼「歌手 歌名」；再退到原始输入。
    """
    keyword = (intent.get("keyword") or "").strip()
    if keyword:
        return keyword

    artist = (intent.get("artist") or "").strip()
    title = (intent.get("title") or "").strip()
    if artist and title:
        return f"{artist} {title}"
    if title:
        return title
    if artist:
        return artist
    return (fallback or "").strip()


def sanitize_recommendation(text: str) -> str:
    """清洗 AI 推荐的歌名，去掉引号、书名号、前缀说明。"""
    value = (text or "").strip()
    value = re.sub(r"^(?:推荐|答案是|建议|歌曲)\s*[:：]?\s*", "", value)

    # 先取第一行，防止模型输出一段解释
    value = value.splitlines()[0].strip() if value else ""

    # 书名号/引号可能出现在中间（《稻香》 周杰伦），不能只用 strip
    value = re.sub(r"[《「“\"]", "", value)
    value = re.sub(r"[》」”\"]", " ", value)
    return re.sub(r"[ \t]+", " ", value).strip()


#: 音源展示名
SOURCE_LABELS = {
    "netease": "网易云",
    "qq": "QQ音乐",
}


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source or "", source or "未知")


def build_caption(song: Song, requester: str = "") -> str:
    """拼发送时的说明文字。"""
    parts = [f"正在播放：{song.display}"]
    if song.duration_ms:
        parts.append(format_duration(song.duration_ms))
    line = "  ".join(parts)
    if requester:
        line += f"\n点歌：{requester}"
    return line
