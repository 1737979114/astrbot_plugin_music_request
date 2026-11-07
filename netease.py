# -*- coding: utf-8 -*-
"""网易云音乐免 Key 接口的轻量封装。

只做两件事：

1. ``search`` 按关键词搜歌（``/api/search/get/web``）
2. ``probe`` 用官方外链地址探测能否播放（``/song/media/outer/url``）

外链地址对非 VIP 歌曲会 302 到 CDN 的 mp3；版权受限或 VIP 独占的歌曲
会 302 到 ``/404``，所以必须逐个探测，不能搜到就直接发。

注意：这里用的是网易云对外开放的分享外链，不涉及账号登录或破解。
VIP 独占、版权下架的歌曲本来就拿不到，属正常现象。
"""

from __future__ import annotations

from typing import Any

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

from music_core import Song


class MusicError(Exception):
    """音乐接口异常。"""


class NeteaseClient:
    """网易云音乐客户端。"""

    SEARCH_URL = "https://music.163.com/api/search/get/web"
    OUTER_URL = "https://music.163.com/song/media/outer/url"

    def __init__(self, timeout: int = 10) -> None:
        try:
            self.timeout = max(3, int(timeout))
        except (TypeError, ValueError):
            self.timeout = 10
        self._http: Any = None

    def _client(self) -> Any:
        if httpx is None:
            raise MusicError("缺少 httpx 依赖，请在插件目录执行 pip install httpx")
        if self._http is None:
            self._http = httpx.AsyncClient(
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    ),
                    "Referer": "https://music.163.com/",
                },
                follow_redirects=False,
                timeout=self.timeout,
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            finally:
                self._http = None

    # ------------------------------------------------------------------ 搜索

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        """按关键词搜歌。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        client = self._client()
        try:
            response = await client.get(
                self.SEARCH_URL,
                params={
                    "s": keyword,
                    "type": 1,
                    "limit": max(1, min(int(limit), 30)),
                    "offset": 0,
                },
            )
        except Exception as exc:
            raise MusicError(f"搜索请求失败（{type(exc).__name__}）") from exc

        if response.status_code != 200:
            raise MusicError(f"搜索接口返回 HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise MusicError("搜索接口返回了非 JSON 内容") from exc

        if not isinstance(payload, dict):
            return []
        if payload.get("code") not in (200, None):
            raise MusicError(f"搜索接口返回 code={payload.get('code')}")

        result = payload.get("result") or {}
        songs = result.get("songs") if isinstance(result, dict) else None
        if not isinstance(songs, list):
            return []

        return [self._to_song(item) for item in songs if isinstance(item, dict)]

    @staticmethod
    def _to_song(item: dict[str, Any]) -> Song:
        artists = []
        artist_cover = ""
        for artist in item.get("artists") or []:
            if isinstance(artist, dict):
                name = str(artist.get("name") or "").strip()
                if name:
                    artists.append(name)
                if not artist_cover:
                    artist_cover = str(
                        artist.get("picUrl") or artist.get("img1v1Url") or ""
                    ).strip()

        album = item.get("album") or {}
        album_name = ""
        cover = ""
        if isinstance(album, dict):
            album_name = str(album.get("name") or "").strip()
            cover = str(album.get("picUrl") or "").strip()
            if not cover:
                # 这个搜索接口不返回 picUrl，但会给歌手头像，拿来当卡片封面
                album_artist = album.get("artist")
                if isinstance(album_artist, dict):
                    cover = str(
                        album_artist.get("img1v1Url")
                        or album_artist.get("picUrl")
                        or ""
                    ).strip()
        if not cover:
            cover = artist_cover

        try:
            duration = int(item.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0

        try:
            fee = int(item.get("fee") or 0)
        except (TypeError, ValueError):
            fee = 0

        return Song(
            id=str(item.get("id") or ""),
            name=str(item.get("name") or "").strip(),
            artists=artists,
            album=album_name,
            duration_ms=duration,
            fee=fee,
            cover=cover,
        )

    # ------------------------------------------------------------------ 探测

    async def probe(self, song_id: str) -> str | None:
        """返回可播放的音频地址；不能播时返回 None。"""
        song_id = str(song_id or "").strip()
        if not song_id:
            return None

        client = self._client()
        url = f"{self.OUTER_URL}?id={song_id}.mp3"

        try:
            response = await client.get(url)
        except Exception as exc:
            raise MusicError(f"探测播放地址失败（{type(exc).__name__}）") from exc

        # 正常情况：302 到 CDN
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            if not location:
                return None
            # 版权受限时会被重定向到 /404
            if "/404" in location or "music.163.com/404" in location:
                return None
            return location

        # 少数情况下直接 200 返回音频
        if response.status_code == 200:
            content_type = response.headers.get("content-type", "")
            if "audio" in content_type or "octet-stream" in content_type:
                return url

        return None
