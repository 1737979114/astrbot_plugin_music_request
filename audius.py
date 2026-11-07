# -*- coding: utf-8 -*-
"""Audius 音源。

Audius 是去中心化的音乐平台，接口完全公开、不需要登录和 API Key，
曲库以独立音乐人、翻唱、Lo-Fi、电子为主（中文歌较少，主要用来补充曲库）。

- 搜索：``GET https://api.audius.co/v1/tracks/search?query=&app_name=``
- 播放：``GET https://api.audius.co/v1/tracks/{id}/stream?app_name=``，302 到 mp3
"""

from __future__ import annotations

from typing import Any

import httpx

from music_core import Song

APP_NAME = "astrbot_music_request"


class AudiusError(Exception):
    """Audius 接口异常。"""


class AudiusClient:
    """Audius 公开 API 客户端。"""

    BASE = "https://api.audius.co"

    def __init__(self, timeout: int = 12) -> None:
        self.timeout = max(5, int(timeout))
        self._http: Any = None

    def _client(self) -> Any:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": "astrbot-music-request/1.0"},
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            finally:
                self._http = None

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        params = {"query": keyword, "app_name": APP_NAME, "limit": max(1, min(limit, 20))}
        try:
            response = await self._client().get(
                f"{self.BASE}/v1/tracks/search", params=params
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise AudiusError(f"搜索失败：{type(exc).__name__}") from exc

        songs: list[Song] = []
        for item in payload.get("data") or []:
            if isinstance(item, dict):
                song = self._to_song(item)
                if song is not None:
                    songs.append(song)
        return songs

    def _to_song(self, item: dict) -> Song | None:
        track_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not track_id or not title:
            return None

        user = item.get("user") or {}
        artist = str(user.get("name") or user.get("handle") or "").strip()
        artwork = item.get("artwork") or {}
        cover = str(
            artwork.get("480x480") or artwork.get("150x150") or ""
        ).strip()

        try:
            duration_ms = int(float(item.get("duration") or 0)) * 1000
        except (TypeError, ValueError):
            duration_ms = 0

        return Song(
            id=track_id,
            name=title,
            artists=[artist] if artist else [],
            album=str(item.get("album_backlink") or ""),
            duration_ms=duration_ms,
            fee=0,
            cover=cover,
            source="audius",
        )

    async def probe(self, song: Song) -> str | None:
        url = f"{self.BASE}/v1/tracks/{song.id}/stream?app_name={APP_NAME}"
        try:
            response = await self._client().get(
                url, headers={"Range": "bytes=0-1024"}
            )
        except Exception as exc:
            raise AudiusError(f"取播放地址失败：{type(exc).__name__}") from exc

        if response.status_code not in (200, 206):
            return None

        content_type = (response.headers.get("content-type") or "").lower()
        if "audio" not in content_type and "octet-stream" not in content_type:
            return None

        return str(response.url)
