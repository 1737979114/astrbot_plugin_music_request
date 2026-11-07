# -*- coding: utf-8 -*-
"""QQ 音乐搜索 + vkey 取流。

免登录能拿到一部分歌（免费/非独家）的播放地址；独家或付费歌的 ``purl`` 为空，
这时返回 None，由上层换下一个候选。

两个接口都不需要签名：

- 搜索：``u.y.qq.com/cgi-bin/musicu.fcg`` 的 ``DoSearchForQQMusicDesktop``
- 取流：同一个网关的 ``CgiGetVkey``，返回 ``purl`` 拼到 ``ws.stream.qqmusic.qq.com`` 即可

注意 vkey 有时效（约两小时），所以每次点歌都实时取，不缓存。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

from music_core import Song


class QQMusicError(Exception):
    """QQ 音乐接口异常。"""


class QQMusicClient:
    """QQ 音乐客户端。"""

    GATEWAY = "https://u.y.qq.com/cgi-bin/musicu.fcg"
    STREAM_BASE = "https://ws.stream.qqmusic.qq.com/"
    COVER_TEMPLATE = "https://y.gtimg.cn/music/photo_new/T002R300x300M000{}.jpg"

    def __init__(self, timeout: int = 10) -> None:
        try:
            self.timeout = max(3, int(timeout))
        except (TypeError, ValueError):
            self.timeout = 10
        self._http: Any = None
        self._warmed = False
        # 网关风控挺严，连续失败就歇一会儿，免得每次点歌都白等
        self._fail_count = 0
        self._blocked_until = 0.0
        self._last_call = 0.0

    def _client(self) -> Any:
        if httpx is None:
            raise QQMusicError("缺少 httpx 依赖，请在插件目录执行 pip install httpx")
        if self._http is None:
            self._http = httpx.AsyncClient(
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    ),
                    "Referer": "https://y.qq.com/",
                },
                follow_redirects=True,
                timeout=self.timeout,
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            finally:
                self._http = None

    async def _warm_up(self) -> None:
        """首次请求前先访问一次首页。

        直接打网关容易被判成爬虫返回 code=2001，先走一遍首页拿 cookie 能稳很多。
        """
        if self._warmed:
            return
        self._warmed = True
        try:
            await self._client().get("https://y.qq.com/")
        except Exception:  # pragma: no cover
            pass

    async def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self._warm_up()
        client = self._client()
        body = {"comm": {"ct": 19, "cv": 1859, "uin": "0"}, **payload}
        try:
            response = await client.get(
                self.GATEWAY,
                params={"format": "json", "data": json.dumps(body)},
            )
        except Exception as exc:
            raise QQMusicError(f"请求失败（{type(exc).__name__}）") from exc

        if response.status_code != 200:
            raise QQMusicError(f"网关返回 HTTP {response.status_code}")

        try:
            data = response.json()
        except ValueError as exc:
            raise QQMusicError("网关返回了非 JSON 内容") from exc

        if not isinstance(data, dict):
            raise QQMusicError("网关返回格式异常")
        return data

    # ------------------------------------------------------------------ 搜索

    async def search(self, keyword: str, limit: int = 10) -> list[Song]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        now = time.monotonic()
        if now < self._blocked_until:
            raise QQMusicError("触发频率限制，暂时跳过")

        # 两次请求之间留间隔，2001 多半是短时间打太密触发的
        wait = 1.5 - (now - self._last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()

        try:
            songs = await self._search_once(keyword, limit)
        except Exception:
            self._fail_count += 1
            # 连续 3 次才熔断，且只停 3 分钟（2001 通常是短时限流）
            if self._fail_count >= 3:
                self._blocked_until = time.monotonic() + 180
            raise

        self._fail_count = 0
        return songs

    async def _search_once(self, keyword: str, limit: int) -> list[Song]:
        payload = {
            "req_1": {
                "module": "music.search.SearchCgiService",
                "method": "DoSearchForQQMusicDesktop",
                "param": {
                    "query": keyword,
                    "num_per_page": max(1, min(int(limit), 30)),
                    "page_num": 1,
                },
            }
        }

        data = await self._call(payload)
        request_data = data.get("req_1") or {}
        if request_data.get("code") not in (0, None):
            raise QQMusicError(f"搜索返回 code={request_data.get('code')}")

        body = (request_data.get("data") or {}).get("body") or {}
        songs = (body.get("song") or {}).get("list")
        if not isinstance(songs, list):
            return []

        return [self._to_song(item) for item in songs if isinstance(item, dict)]

    def _to_song(self, item: dict[str, Any]) -> Song:
        artists = []
        for singer in item.get("singer") or []:
            if isinstance(singer, dict):
                name = str(singer.get("name") or "").strip()
                if name:
                    artists.append(name)

        album = item.get("album") or {}
        album_name = ""
        album_mid = ""
        if isinstance(album, dict):
            album_name = str(album.get("name") or "").strip()
            album_mid = str(album.get("mid") or "").strip()

        try:
            interval = int(item.get("interval") or 0)
        except (TypeError, ValueError):
            interval = 0

        return Song(
            id=str(item.get("mid") or "").strip(),
            name=str(item.get("name") or "").strip(),
            artists=artists,
            album=album_name,
            duration_ms=interval * 1000,
            # QQ 音乐不返回 fee 字段，置 0 不额外加分
            fee=0,
            cover=self.COVER_TEMPLATE.format(album_mid) if album_mid else "",
            source="qq",
        )

    # ------------------------------------------------------------------ 取流

    async def probe(self, song: Song) -> str | None:
        """返回可播放地址；未登录拿不到的版权歌返回 None。"""
        mid = str(getattr(song, "id", "") or "").strip()
        if not mid:
            return None

        payload = {
            "req_0": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "guid": "10000",
                    "songmid": [mid],
                    "songtype": [0],
                    "uin": "0",
                    "loginflag": 1,
                    "platform": "20",
                },
            }
        }

        data = await self._call(payload)
        data_node = (data.get("req_0") or {}).get("data") or {}
        infos = data_node.get("midurlinfo") or []
        if not infos or not isinstance(infos[0], dict):
            return None

        purl = str(infos[0].get("purl") or "").strip()
        if not purl:
            return None
        if purl.startswith("http"):
            return purl
        return self.STREAM_BASE + purl
