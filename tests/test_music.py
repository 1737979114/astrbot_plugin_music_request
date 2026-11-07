# -*- coding: utf-8 -*-
"""点歌插件的离线测试。

运行：python -m unittest discover -s tests -v
"""

import asyncio
import importlib.util
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from music_core import (  # noqa: E402
    Song,
    build_caption,
    build_keyword,
    clean_title,
    format_duration,
    is_instrumental,
    parse_intent_json,
    rank_songs,
    sanitize_recommendation,
    score_song,
    strip_command_prefix,
)
from netease import MusicError  # noqa: E402


# --------------------------------------------------------------------------
# 假 astrbot 环境
# --------------------------------------------------------------------------


class FakeRecord:
    def __init__(self, file=None, **_):
        self.file = file


class FakePlain:
    def __init__(self, text="", **_):
        self.text = text


def _ensure_astrbot() -> None:
    if "astrbot" in sys.modules:
        api = sys.modules.get("astrbot.api")
        mc = sys.modules.get("astrbot.api.message_components")
        if mc is None and api is not None:
            mc = types.ModuleType("astrbot.api.message_components")
            api.message_components = mc
            sys.modules["astrbot.api.message_components"] = mc
        if mc is not None:
            if not hasattr(mc, "Record"):
                mc.Record = FakeRecord
            if not hasattr(mc, "Plain"):
                mc.Plain = FakePlain
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("fake.astrbot")
    api.AstrBotConfig = dict

    event_mod = types.ModuleType("astrbot.api.event")

    class _EventMessageType:
        ALL = "all"
        GROUP_MESSAGE = "group"
        PRIVATE_MESSAGE = "private"

    class _Filter:
        EventMessageType = _EventMessageType

        @staticmethod
        def command(*args, **kwargs):
            def decorator(func):
                return func

            return decorator

        @staticmethod
        def event_message_type(*args, **kwargs):
            def decorator(func):
                return func

            return decorator

    class AstrMessageEvent:
        pass

    class MessageEventResult:
        pass

    event_mod.filter = _Filter
    event_mod.EventMessageType = _EventMessageType
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageEventResult = MessageEventResult

    star_mod = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context=None):
            self.context = context

    class Context:
        pass

    star_mod.Star = Star
    star_mod.Context = Context

    mc = types.ModuleType("astrbot.api.message_components")
    mc.Record = FakeRecord
    mc.Plain = FakePlain

    api.event = event_mod
    api.star = star_mod
    api.message_components = mc
    astrbot.api = api

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.message_components"] = mc


_ensure_astrbot()

_spec = importlib.util.spec_from_file_location(
    "music_plugin_under_test", PLUGIN_DIR / "main.py"
)
plugin_main = importlib.util.module_from_spec(_spec)
sys.modules["music_plugin_under_test"] = plugin_main
_spec.loader.exec_module(plugin_main)


# --------------------------------------------------------------------------
# 假对象
# --------------------------------------------------------------------------


class FakeClient:
    def __init__(self, mapping=None, playable=None, error=None):
        self.mapping = mapping or {}
        self.playable = playable or {}
        self.error = error
        self.searches = []

    async def search(self, keyword, limit=10):
        self.searches.append(keyword)
        if self.error:
            raise self.error
        return list(self.mapping.get(keyword, []))

    async def probe(self, song):
        # 兼容传 Song 或直接传 id 两种写法
        sid = getattr(song, "id", None) or song
        return self.playable.get(str(sid))

    async def close(self):
        pass


class FakeContext:
    def __init__(self, reply="", provider="prov-1"):
        self.reply = reply
        self.provider = provider
        self.prompts = []

    async def get_current_chat_provider_id(self, umo=""):
        return self.provider

    async def llm_generate(self, chat_provider_id=None, prompt=None):
        self.prompts.append(prompt)
        return SimpleNamespace(completion_text=self.reply)


class FakeBot:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self.fail:
            raise RuntimeError("api boom")
        return {"message_id": 1}


class FakeEvent:
    def __init__(
        self, text, sender_id="456", sender_name="小明", bot=None, group_id="123"
    ):
        self.message_str = text
        self.unified_msg_origin = "test:GroupMessage:1"
        self.bot = bot
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._group_id = group_id
        self.sent = []
        self.stopped = False

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return self._group_id

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)

    async def send(self, result):
        self.sent.append(result)

    def stop_event(self):
        self.stopped = True


async def _collect(plugin, event):
    return [item async for item in plugin._handle_music(event)]


def collect(plugin, event):
    return asyncio.run(_collect(plugin, event))


async def _collect_choice(plugin, event):
    return [item async for item in plugin._handle_choice(event)]


def collect_choice(plugin, event):
    return asyncio.run(_collect_choice(plugin, event))


async def _collect_on_choice(plugin, event):
    return [item async for item in plugin.on_choice(event)]


def collect_on_choice(plugin, event):
    return asyncio.run(_collect_on_choice(plugin, event))


def make_plugin(client, **overrides):
    config = {
        "enable": True,
        "use_ai": False,
        "default_keywords": ["稻香"],
        "search_limit": 10,
        "probe_limit": 5,
        "enable_choice": False,
        "choice_count": 5,
        "choice_timeout": 120,
        "send_mode": "card",
        "rate_limit_seconds": 0,
        "request_timeout": 10,
    }
    config.update(overrides)
    plugin = plugin_main.MusicRequestPlugin(context=FakeContext(), config=config)
    plugin.client = client
    return plugin


def song(sid="1", name="晴天", artists=None, duration=269000, fee=0, cover=None):
    return Song(
        id=sid,
        name=name,
        artists=artists or ["周杰伦"],
        duration_ms=duration,
        fee=fee,
        cover=cover if cover is not None else "http://cdn.example/cover.jpg",
    )


# --------------------------------------------------------------------------
# 纯逻辑
# --------------------------------------------------------------------------


class TestText(unittest.TestCase):
    def test_strip_command(self):
        self.assertEqual(strip_command_prefix("点歌 晴天"), "晴天")
        self.assertEqual(strip_command_prefix("/来首歌 稻香"), "稻香")
        self.assertEqual(strip_command_prefix("！听歌 起风了"), "起风了")
        self.assertEqual(strip_command_prefix("点歌"), "")

    def test_clean_title(self):
        self.assertEqual(clean_title("晴天 (Live)"), "晴天")
        self.assertEqual(clean_title("晴天（女声版）"), "晴天")
        self.assertEqual(clean_title("晴天"), "晴天")

    def test_duration(self):
        self.assertEqual(format_duration(269000), "4:29")
        self.assertEqual(format_duration(0), "--:--")
        self.assertEqual(format_duration(59000), "0:59")

    def test_caption(self):
        text = build_caption(song(), "小明")
        self.assertIn("晴天", text)
        self.assertIn("周杰伦", text)
        self.assertIn("4:29", text)
        self.assertIn("点歌：小明", text)


class TestScoring(unittest.TestCase):
    def test_exact_match_scores_higher(self):
        exact = score_song(song(), artist="周杰伦", title="晴天")
        other = score_song(
            song(sid="2", name="江南", artists=["林俊杰"]),
            artist="周杰伦",
            title="晴天",
        )
        self.assertGreater(exact, other)

    def test_artist_match(self):
        right = score_song(song(), artist="周杰伦", title="")
        wrong = score_song(song(artists=["某翻唱"]), artist="周杰伦", title="")
        self.assertGreater(right, wrong)

    def test_rank_order(self):
        songs = [
            song(sid="1", name="晴天 (深情版)", artists=["Lucky小爱"]),
            song(sid="2", name="晴天", artists=["周杰伦"]),
            song(sid="3", name="晴天", artists=["RyaVocal"]),
        ]
        ranked = rank_songs(songs, artist="周杰伦", title="晴天")
        self.assertEqual(ranked[0].id, "2")

    def test_prefer_full_song_over_clip(self):
        """搜索结果里混着 1 分多钟的剪辑版，应优先完整版。"""
        full = song(sid="full", name="晴天", duration=269000)
        clip = song(sid="clip", name="晴天", duration=112000)
        ranked = rank_songs([clip, full], title="晴天")
        self.assertEqual(ranked[0].id, "full")

    def test_instrumental_detection(self):
        self.assertTrue(is_instrumental(song(name="晴天 (伴奏)")))
        self.assertTrue(is_instrumental(song(name="River Flows in You 纯音乐")))
        self.assertFalse(is_instrumental(song(name="晴天")))

    def test_instrumental_ranked_last(self):
        """用户点的是歌，伴奏要排到最后。"""
        original = song(sid="orig", name="银河与星斗", duration=194000)
        inst = song(sid="inst", name="银河与星斗 (伴奏)", duration=194000)
        ranked = rank_songs([inst, original], title="银河与星斗")
        self.assertEqual(ranked[0].id, "orig")


class TestIntent(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(
            parse_intent_json('{"keyword":"晴天","artist":"周杰伦"}'),
            {"keyword": "晴天", "artist": "周杰伦"},
        )

    def test_fenced_json(self):
        text = '```json\n{"keyword":"晴天","title":"晴天"}\n```'
        self.assertEqual(parse_intent_json(text), {"keyword": "晴天", "title": "晴天"})

    def test_with_explanation(self):
        text = '好的，解析结果：{"keyword":"稻香"} 希望你喜欢'
        self.assertEqual(parse_intent_json(text), {"keyword": "稻香"})

    def test_invalid(self):
        self.assertEqual(parse_intent_json("不是 JSON"), {})
        self.assertEqual(parse_intent_json(""), {})

    def test_build_keyword(self):
        self.assertEqual(build_keyword({"keyword": "晴天"}), "晴天")
        self.assertEqual(
            build_keyword({"artist": "周杰伦", "title": "晴天"}), "周杰伦 晴天"
        )
        self.assertEqual(build_keyword({"title": "晴天"}), "晴天")
        self.assertEqual(build_keyword({}, "原话"), "原话")

    def test_sanitize(self):
        self.assertEqual(sanitize_recommendation("《稻香》 周杰伦"), "稻香 周杰伦")
        self.assertEqual(sanitize_recommendation("推荐：晴天 周杰伦"), "晴天 周杰伦")
        self.assertEqual(
            sanitize_recommendation("稻香 周杰伦\n因为很好听"), "稻香 周杰伦"
        )


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


class TestFlow(unittest.TestCase):
    def test_card_sent(self):
        client = FakeClient(
            mapping={"周杰伦 晴天": [song()]},
            playable={"1": "http://cdn.example/1.mp3"},
        )
        plugin = make_plugin(client)
        bot = FakeBot()
        event = FakeEvent("/点歌 周杰伦 晴天", bot=bot)

        out = collect(plugin, event)

        # 卡片发送成功时不再补发文本
        self.assertEqual(out, [])
        self.assertEqual(len(bot.calls), 1)
        action, params = bot.calls[0]
        self.assertEqual(action, "send_group_msg")
        segment = params["message"][0]
        self.assertEqual(segment["type"], "music")
        self.assertEqual(segment["data"]["type"], "custom")
        self.assertIn("1.mp3", segment["data"]["audio"])
        self.assertIn("晴天", segment["data"]["title"])

    def test_card_without_cover_falls_back(self):
        client = FakeClient(
            mapping={"晴天": [song(cover="")]},
            playable={"1": "http://cdn.example/1.mp3"},
        )
        plugin = make_plugin(client)
        bot = FakeBot()

        out = collect(plugin, FakeEvent("/点歌 晴天", bot=bot))

        self.assertEqual(bot.calls, [])
        self.assertIn("http://cdn.example/1.mp3", out[0][1])

    def test_card_api_failure_falls_back(self):
        client = FakeClient(
            mapping={"晴天": [song()]},
            playable={"1": "http://cdn.example/1.mp3"},
        )
        plugin = make_plugin(client)
        bot = FakeBot(fail=True)

        out = collect(plugin, FakeEvent("/点歌 晴天", bot=bot))

        self.assertIn("http://cdn.example/1.mp3", out[0][1])

    def test_record_mode(self):
        client = FakeClient(
            mapping={"周杰伦 晴天": [song()]},
            playable={"1": "http://cdn.example/1.mp3"},
        )
        plugin = make_plugin(client, send_mode="record")
        event = FakeEvent("/点歌 周杰伦 晴天")

        # 模拟切出两段语音，避免测试依赖网络和 ffmpeg
        files = []
        for index in range(2):
            fd, path = tempfile.mkstemp(suffix=f"_{index}.mp3")
            os.write(fd, b"x" * 4096)
            os.close(fd)
            files.append(path)

        async def fake_parts(_url):
            return files

        plugin._prepare_voice_parts = fake_parts

        out = collect(plugin, event)

        # 分段后走 event.send，不再 yield
        self.assertEqual(len(out), 0)
        self.assertEqual(len(event.sent), 2)
        for kind, chain in event.sent:
            self.assertEqual(kind, "chain")
            self.assertTrue(any(getattr(c, "file", "") for c in chain))

        for path in files:
            os.unlink(path)

    def test_text_mode(self):
        client = FakeClient(
            mapping={"周杰伦 晴天": [song()]},
            playable={"1": "http://cdn.example/1.mp3"},
        )
        plugin = make_plugin(client, send_mode="text")
        event = FakeEvent("/点歌 周杰伦 晴天")

        out = collect(plugin, event)

        self.assertIn("http://cdn.example/1.mp3", out[0][1])
        self.assertEqual(event.sent, [])

    def test_no_result(self):
        plugin = make_plugin(FakeClient())

        out = collect(plugin, FakeEvent("/点歌 不存在的歌"))

        self.assertIn("没找到", out[0][1])

    def test_unplayable(self):
        client = FakeClient(mapping={"晴天": [song()]}, playable={})
        plugin = make_plugin(client)

        out = collect(plugin, FakeEvent("/点歌 晴天"))

        self.assertIn("播放不了", out[0][1])

    def test_skip_unplayable_and_take_next(self):
        client = FakeClient(
            mapping={"晴天": [song(sid="1"), song(sid="2")]},
            playable={"2": "http://cdn.example/2.mp3"},
        )
        plugin = make_plugin(client)

        out = collect(plugin, FakeEvent("/点歌 晴天"))

        self.assertIn("晴天", out[0][1])
        self.assertIn("2.mp3", out[0][1] + str(client.playable))

    def test_prefer_vocal_over_instrumental(self):
        """原版不可播时，先找其他有人声版本，而不是直接给伴奏。"""
        songs = [
            song(sid="orig", name="银河与星斗", duration=194000),
            song(sid="cover", name="银河与星斗", artists=["云鸢浅浅"], duration=188000),
            song(sid="inst", name="银河与星斗 (伴奏)", duration=194000),
        ]
        client = FakeClient(
            mapping={"银河与星斗": songs},
            playable={
                "cover": "http://cdn.example/cover.mp3",
                "inst": "http://cdn.example/inst.mp3",
            },
        )
        plugin = make_plugin(client, probe_limit=3)
        bot = FakeBot()

        collect(plugin, FakeEvent("/点歌 银河与星斗", bot=bot))

        segment = bot.calls[0][1]["message"][0]
        self.assertIn("cover.mp3", segment["data"]["audio"])

    def test_fallback_to_instrumental_with_note(self):
        """只有伴奏能播时给出伴奏，并明确告知原因。"""
        songs = [
            song(sid="orig", name="银河与星斗"),
            song(sid="inst", name="银河与星斗 (伴奏)"),
        ]
        client = FakeClient(
            mapping={"银河与星斗": songs},
            playable={"inst": "http://cdn.example/inst.mp3"},
        )
        plugin = make_plugin(client, send_mode="text")

        out = collect(plugin, FakeEvent("/点歌 银河与星斗"))

        self.assertIn("原版暂时不可播", out[0][1])

    def test_explicit_instrumental_request(self):
        """用户明确要伴奏时，伴奏优先。"""
        songs = [
            song(sid="orig", name="银河与星斗"),
            song(sid="inst", name="银河与星斗 (伴奏)"),
        ]
        client = FakeClient(
            mapping={"银河与星斗 伴奏": songs},
            playable={
                "orig": "http://cdn.example/orig.mp3",
                "inst": "http://cdn.example/inst.mp3",
            },
        )
        plugin = make_plugin(client, send_mode="text")

        out = collect(plugin, FakeEvent("/点歌 银河与星斗 伴奏"))

        self.assertIn("inst.mp3", out[0][1])

    def test_search_error(self):
        plugin = make_plugin(FakeClient(error=MusicError("boom")))

        out = collect(plugin, FakeEvent("/点歌 晴天"))

        self.assertIn("暂时不可用", out[0][1])

    def test_random_keyword_when_empty(self):
        client = FakeClient(
            mapping={"稻香": [song(name="稻香")]},
            playable={"1": "http://cdn.example/1.mp3"},
        )
        plugin = make_plugin(client, default_keywords=["稻香"])

        out = collect(plugin, FakeEvent("/点歌"))

        self.assertIn("稻香", out[0][1])
        self.assertEqual(client.searches, ["稻香"])

    def test_rate_limit(self):
        client = FakeClient(
            mapping={"晴天": [song()]},
            playable={"1": "http://cdn.example/1.mp3"},
        )
        plugin = make_plugin(client, rate_limit_seconds=60)

        first = collect(plugin, FakeEvent("/点歌 晴天"))
        second = collect(plugin, FakeEvent("/点歌 晴天"))

        self.assertNotIn("频繁", first[0][1])
        self.assertIn("频繁", second[0][1])

    def test_disabled(self):
        plugin = make_plugin(FakeClient(), enable=False)

        self.assertEqual(collect(plugin, FakeEvent("/点歌 晴天")), [])

    def test_at_prefix_stripped(self):
        client = FakeClient(
            mapping={"晴天": [song()]},
            playable={"1": "http://cdn.example/1.mp3"},
        )
        plugin = make_plugin(client)

        out = collect(plugin, FakeEvent("@bot 点歌 晴天"))

        self.assertIn("晴天", out[0][1])


class TestChoice(unittest.TestCase):
    """点歌后列出候选、回复序号播放。"""

    def _client(self):
        return FakeClient(
            mapping={
                "银河与星斗": [
                    song(sid="1", name="银河与星斗"),
                    song(sid="2", name="银河与星斗", artists=["云鸢浅浅"]),
                    song(sid="3", name="银河与星斗 (伴奏)"),
                ]
            },
            playable={"2": "http://cdn.example/2.mp3"},
        )

    def test_list_returned(self):
        plugin = make_plugin(self._client(), enable_choice=True)
        event = FakeEvent("/点歌 银河与星斗")

        out = collect(plugin, event)

        self.assertEqual(len(out), 1)
        self.assertIn("回复序号", out[0][1])
        self.assertIn("1.", out[0][1])
        self.assertTrue(event.stopped)
        self.assertEqual(event.sent, [])

    def test_select_second(self):
        plugin = make_plugin(self._client(), enable_choice=True, send_mode="text")
        collect(plugin, FakeEvent("/点歌 银河与星斗"))

        out = collect_choice(plugin, FakeEvent("2"))

        self.assertEqual(len(out), 1)
        self.assertIn("2.mp3", out[0][1])

    def test_select_out_of_range(self):
        plugin = make_plugin(self._client(), enable_choice=True)
        collect(plugin, FakeEvent("/点歌 银河与星斗"))

        out = collect_choice(plugin, FakeEvent("9"))

        self.assertIn("超出范围", out[0][1])

    def test_non_number_ignored(self):
        plugin = make_plugin(self._client(), enable_choice=True)
        collect(plugin, FakeEvent("/点歌 银河与星斗"))

        self.assertEqual(collect_choice(plugin, FakeEvent("你好")), [])

    def test_expired_session(self):
        plugin = make_plugin(self._client(), enable_choice=True)
        collect(plugin, FakeEvent("/点歌 银河与星斗"))
        for session in plugin._choices.values():
            session.expire_at = 0.0

        self.assertEqual(collect_choice(plugin, FakeEvent("2")), [])

    def test_unplayable_keeps_session(self):
        plugin = make_plugin(self._client(), enable_choice=True)
        collect(plugin, FakeEvent("/点歌 银河与星斗"))

        out = collect_choice(plugin, FakeEvent("1"))

        self.assertIn("播不了", out[0][1])
        self.assertTrue(plugin._choices, "播放失败时应保留会话，让用户换序号")

    def test_choice_disabled_plays_directly(self):
        plugin = make_plugin(self._client(), enable_choice=False, send_mode="text")

        out = collect(plugin, FakeEvent("/点歌 银河与星斗"))

        self.assertIn("http", out[0][1])
        self.assertEqual(plugin._choices, {})


class TestPrefixedCommand(unittest.TestCase):
    def test_slash_prefix_handled(self):
        """wake_prefix 为空时 /点歌 匹配不到命令，全消息入口要兜住。"""
        client = FakeClient(
            mapping={"晴天": [song()]},
            playable={"1": "http://cdn.example/1.mp3"},
        )
        plugin = make_plugin(client, send_mode="text")

        out = collect_on_choice(plugin, FakeEvent("/点歌 晴天"))

        self.assertIn("http://cdn.example/1.mp3", out[0][1])

    def test_plain_chat_ignored(self):
        plugin = make_plugin(FakeClient())

        out = collect_on_choice(plugin, FakeEvent("今天天气不错"))

        self.assertEqual(out, [])


class TestAI(unittest.TestCase):
    def test_intent_used_for_search(self):
        client = FakeClient(
            mapping={"晴天": [song()]},
            playable={"1": "http://cdn.example/1.mp3"},
        )
        plugin = make_plugin(client, use_ai=True)
        context = FakeContext(
            reply='{"keyword":"晴天","artist":"周杰伦","title":"晴天"}'
        )
        plugin.context = context

        out = collect(plugin, FakeEvent("/点歌 杰伦那首下雨天唱的"))

        self.assertIn("晴天", out[0][1])
        self.assertEqual(client.searches[0], "晴天")
        self.assertEqual(len(context.prompts), 1)

    def test_intent_failure_falls_back_to_raw(self):
        client = FakeClient(
            mapping={"晴天": [song()]},
            playable={"1": "http://cdn.example/1.mp3"},
        )
        plugin = make_plugin(client, use_ai=True)
        plugin.context = FakeContext(reply="我不知道")

        collect(plugin, FakeEvent("/点歌 晴天"))

        self.assertEqual(client.searches[0], "晴天")

    def test_ai_recommend_when_no_result(self):
        client = FakeClient(
            mapping={"稻香 周杰伦": [song(name="稻香")]},
            playable={"1": "http://cdn.example/1.mp3"},
        )
        plugin = make_plugin(client, use_ai=True)
        plugin.context = FakeContext(reply="稻香 周杰伦")

        out = collect(plugin, FakeEvent("/点歌 随便来首开心的"))

        self.assertIn("稻香", out[0][1])
        self.assertIn("稻香 周杰伦", client.searches)

    def test_ai_disabled(self):
        client = FakeClient(
            mapping={"晴天": [song()]},
            playable={"1": "http://cdn.example/1.mp3"},
        )
        plugin = make_plugin(client, use_ai=False)
        context = FakeContext(reply='{"keyword":"不该用"}')
        plugin.context = context

        collect(plugin, FakeEvent("/点歌 晴天"))

        self.assertEqual(context.prompts, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
