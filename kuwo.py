# -*- coding: utf-8 -*-
"""酷我音乐音源。

只用酷我网页端公开的搜索/播放接口，不需要登录、不碰会员鉴权：

- 搜索：``http://search.kuwo.cn/r.s``
- 播放：``http://antiserver.kuwo.cn/anti.s`` 返回一个可直接下载的 mp3 地址

能覆盖到的以免费曲目、翻唱、伴奏、合集为主；版权受限的原唱可能只有片段，
探测阶段会做一次可达性检查，拿不到就跳过，交给其他音源。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from music_core import Song


class KuwoError(Exception):
    """酷我接口异常。"""


class KuwoClient:
    """酷我免登录客户端。"""

    SEARCH_URL = "http://search.kuwo.cn/r.s"
    PLAY_URL = "http://antiserver.kuwo.cn/anti.s"
    COVER_BASE = "https://img1.kuwo.cn/star/albumcover/"

    def __init__(self, timeout: int = 10) -> None:
        self.timeout = max(5, int(timeout))
        self._http: Any = None

    def _client(self) -> Any:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
                    ),
                    "Referer": "http://www.kuwo.cn/",
                },
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            finally:
                self._http = None

    # ---------------------------------------------------------------- 搜索

    @staticmethod
    def _loads(text: str) -> dict:
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except ValueError:
            pass
        try:
            data = json.loads(text.replace("'", '"'))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        params = {
            "all": keyword,
            "ft": "music",
            "itemset": "web_2013",
            "client": "kt",
            "pn": 0,
            "rn": max(1, min(limit, 30)),
            "rformat": "json",
            "encoding": "utf8",
        }
        try:
            response = await self._client().get(self.SEARCH_URL, params=params)
            response.raise_for_status()
        except Exception as exc:
            raise KuwoError(f"搜索失败：{type(exc).__name__}") from exc

        data = self._loads(response.text)
        items = data.get("abslist") or []
        songs: list[Song] = []
        for item in items:
            if isinstance(item, dict):
                song = self._to_song(item)
                if song is not None:
                    songs.append(song)
        return songs

    def _to_song(self, item: dict) -> Song | None:
        rid = str(item.get("MUSICRID") or "").replace("MUSIC_", "").strip()
        name = self._clean(str(item.get("SONGNAME") or ""))
        if not rid or not name:
            return None

        artists = [
            self._clean(part)
            for part in str(item.get("ARTIST") or "").split("&")
            if self._clean(part)
        ]
        album = self._clean(str(item.get("ALBUM") or ""))

        try:
            duration_ms = int(float(item.get("DURATION") or 0)) * 1000
        except (TypeError, ValueError):
            duration_ms = 0

        return Song(
            id=rid,
            name=name,
            artists=artists,
            album=album,
            duration_ms=duration_ms,
            fee=0,
            cover="",
            source="kuwo",
        )

    @staticmethod
    def _clean(text: str) -> str:
        return (
            text.replace("&nbsp;", " ")
            .replace("\\u0026", "&")
            .replace("\\&", "&")
            .replace("\\u003c", "<")
            .replace("\\u003e", ">")
            .strip()
        )

    # ---------------------------------------------------------------- 播放

    async def probe(self, song: Song) -> str | None:
        params = {
            "type": "convert_url",
            "format": "mp3",
            "response": "url",
            "rid": f"MUSIC_{song.id}",
        }
        try:
            response = await self._client().get(self.PLAY_URL, params=params)
            url = response.text.strip()
        except Exception as exc:
            raise KuwoError(f"取播放地址失败：{type(exc).__name__}") from exc

        if not url.startswith("http"):
            return None

        # 有的链接会返回 404 或者空文件，探测一下再交给上层
        try:
            head = await self._client().head(url)
            content_type = (head.headers.get("content-type") or "").lower()
            if head.status_code >= 400:
                return None
            if "audio" not in content_type and "octet-stream" not in content_type:
                return None
        except Exception:
            # 部分 CDN 不支持 HEAD，放行让上层去试
            return url

        return url
