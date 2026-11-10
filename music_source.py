# -*- coding: utf-8 -*-
"""音源聚合层。

把多个音乐源包成统一接口，点歌时按配置顺序依次搜索，合并结果；
播放探测按歌曲的来源分发到对应客户端。

新增音源只需要实现 :class:`MusicSource` 的三个方法，然后在
:class:`MusicLibrary` 里注册即可。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from music_core import Song
from audius import AudiusClient
from kuwo import KuwoClient
from netease import NeteaseClient
from qq import QQMusicClient


class MusicSource:
    """音源基类。"""

    name = ""
    label = ""

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        raise NotImplementedError

    async def probe(self, song: Song) -> str | None:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class AudiusSource(MusicSource):
    """Audius：完全开放的独立音乐平台，补充英文/电子/翻唱。"""

    name = "audius"
    label = "Audius"

    def __init__(self, timeout: int = 12) -> None:
        self.client = AudiusClient(timeout=timeout)

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        return await self.client.search(keyword, limit=limit)

    async def probe(self, song: Song) -> str | None:
        return await self.client.probe(song)

    async def close(self) -> None:
        await self.client.close()


class KuwoSource(MusicSource):
    """酷我：免登录，免费曲目、翻唱、伴奏覆盖比较全。"""

    name = "kuwo"
    label = "酷我"

    def __init__(self, timeout: int = 10) -> None:
        self.client = KuwoClient(timeout=timeout)

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        return await self.client.search(keyword, limit=limit)

    async def probe(self, song: Song) -> str | None:
        return await self.client.probe(song)

    async def close(self) -> None:
        await self.client.close()


class NeteaseSource(MusicSource):
    """网易云：免 Key，外链可直接播，覆盖面偏版权。"""

    name = "netease"
    label = "网易云"

    def __init__(self, timeout: int = 10) -> None:
        self.client = NeteaseClient(timeout=timeout)

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        songs = await self.client.search(keyword, limit=limit)
        for song in songs:
            song.source = self.name
        return songs

    async def probe(self, song: Song) -> str | None:
        return await self.client.probe(song.id)

    async def close(self) -> None:
        await self.client.close()


class QQSource(MusicSource):
    """QQ 音乐：搜索覆盖更全，部分歌未登录也能取到 vkey。"""

    name = "qq"
    label = "QQ音乐"

    def __init__(self, timeout: int = 10) -> None:
        self.client = QQMusicClient(timeout=timeout)

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        return await self.client.search(keyword, limit=limit)

    async def probe(self, song: Song) -> str | None:
        return await self.client.probe(song)

    async def close(self) -> None:
        await self.client.close()


#: 支持的内置音源
SOURCE_CLASSES: dict[str, type[MusicSource]] = {
    "netease": NeteaseSource,
    "qq": QQSource,
    "kuwo": KuwoSource,
    "audius": AudiusSource,
}

#: 默认开启的音源（按优先级）
DEFAULT_SOURCES = ("netease", "qq", "kuwo")


class MusicLibrary:
    """多音源聚合。"""

    def __init__(self, sources: list[str] | None = None, timeout: int = 10) -> None:
        names = [
            str(item).strip().lower() for item in (sources or []) if str(item).strip()
        ]
        if not names:
            names = list(DEFAULT_SOURCES)

        self.sources: dict[str, MusicSource] = {}
        self.order: list[str] = []
        for name in names:
            if name in self.sources or name not in SOURCE_CLASSES:
                continue
            try:
                self.sources[name] = SOURCE_CLASSES[name](timeout=timeout)
                self.order.append(name)
            except Exception as exc:  # pragma: no cover
                logger.warning("[music] 初始化音源 %s 失败：%s", name, exc)

        # 配置写错了也不至于完全没音源
        if not self.sources:
            self.sources["netease"] = NeteaseSource(timeout=timeout)
            self.order = ["netease"]

    @property
    def labels(self) -> str:
        return "、".join(self.sources[name].label for name in self.order)

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        """并发搜索所有音源，合并结果。

        并发是为了不让慢的音源拖住整体；单个源出错只丢那部分候选。
        """
        if not self.order:
            return []

        parts = await asyncio.gather(
            *(self._safe_search(name, keyword, limit) for name in self.order)
        )

        results: list[Song] = []
        for part in parts:
            results.extend(part)
        return results

    async def _safe_search(self, name: str, keyword: str, limit: int) -> list[Song]:
        try:
            return await self.sources[name].search(keyword, limit=limit)
        except Exception as exc:
            logger.warning("[music] 音源 %s 搜索失败：%s", name, exc)
            return []

    async def probe(self, song: Song) -> str | None:
        """按歌曲来源分发探测。"""
        source = self.sources.get(getattr(song, "source", "") or "")
        if source is None:
            return None
        return await source.probe(song)

    async def close(self) -> None:
        for source in self.sources.values():
            try:
                await source.close()
            except Exception as exc:  # pragma: no cover
                logger.debug("[music] 关闭音源失败：%s", exc)


def build_library(config_value: Any, timeout: int = 10) -> MusicLibrary:
    """从配置构造聚合器，兼容字符串和列表。"""
    if isinstance(config_value, str):
        names = [item.strip() for item in config_value.split(",") if item.strip()]
    elif isinstance(config_value, (list, tuple, set)):
        names = [str(item).strip() for item in config_value if str(item).strip()]
    else:
        names = []
    return MusicLibrary(names, timeout=timeout)
