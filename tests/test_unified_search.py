"""统一 Steam 搜索和简约输出的回归测试，不发起网络请求。"""

import asyncio
import base64
import importlib
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def _decorator(*args, **kwargs):
    return lambda func: func


astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
event_module = types.ModuleType("astrbot.api.event")
star_module = types.ModuleType("astrbot.api.star")
api.logger = _Logger()
event_module.filter = types.SimpleNamespace(command=_decorator, regex=_decorator)
event_module.AstrMessageEvent = type("AstrMessageEvent", (), {})
star_module.Context = type("Context", (), {})
star_module.Star = type("Star", (), {})
star_module.StarTools = type("StarTools", (), {})
sys.modules.update({
    "astrbot": astrbot, "astrbot.api": api,
    "astrbot.api.event": event_module, "astrbot.api.star": star_module,
})

plugin_module = importlib.import_module("astrbot_plugin_steam_radar.main")
models = importlib.import_module("astrbot_plugin_steam_radar.models.store_models")
formatter = importlib.import_module("astrbot_plugin_steam_radar.core.formatter")
router = importlib.import_module("astrbot_plugin_steam_radar.core.search_router")
steam_client = importlib.import_module("astrbot_plugin_steam_radar.core.steam_client")


class _Result:
    def __init__(self):
        self.text = ""
        self.image = None

    def message(self, text):
        self.text = text
        return self

    def url_image(self, url):
        self.image = url
        return self

    def base64_image(self, data):
        self.image = f"base64://{data}"
        return self


class _Event:
    unified_msg_origin = "test:GroupMessage:1"

    def __init__(self, message):
        self.message_str = message

    def make_result(self):
        return _Result()

    def plain_result(self, text):
        return _Result().message(text)

    def get_sender_id(self):
        return "user1"


class _ACL:
    async def check_access(self, _origin):
        return True


class _Client:
    def __init__(self, results):
        self.results = results
        self.calls = []
        self.params = []

    async def download_bytes(self, url):
        return b"\xff\xd8\xfftest-cover"

    async def search_suggest(self, keyword, cc, lang):
        self.calls.append(keyword)
        self.params.append((keyword, cc, lang))
        if isinstance(self.results, Exception):
            raise self.results
        return self.results


class SearchTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, results, enhanced=False):
        plugin = object.__new__(plugin_module.SteamStoreSniperPlugin)
        plugin.config = {"enhanced_search": enhanced, "itad_api_key": "test" if enhanced else ""}
        plugin._acl = _ACL()
        plugin._client = _Client(results)
        plugin._itad_client = object() if enhanced else None
        plugin._search_select_cache = {}
        plugin._SEARCH_CACHE_TTL = 120
        plugin._session_review_lang = {}
        plugin.queries = []

        async def query(appid, cc, lang, **kwargs):
            plugin.queries.append(appid)
            return models.SteamGameInfo(
                steam_appid=appid, name=f"Game {appid}", header_image="https://example.test/cover.jpg"
            ), cc

        async def forbidden(*args, **kwargs):
            raise AssertionError("Short numeric search must not use AI or ITAD")

        plugin._query_with_fallback = query
        plugin._llm_validate_search = forbidden
        plugin._translate_to_english = forbidden
        return plugin

    async def run_command(self, plugin, message):
        return [item async for item in plugin.cmd_steam(_Event(message))]

    async def test_short_numeric_name_result_skips_appid_and_ai(self):
        item = {"type": "game", "appid": 201, "name": "12", "price": "$2", "image_url": ""}
        plugin = self.make_plugin([item], enhanced=True)
        output = await self.run_command(plugin, "/steam 12")
        self.assertEqual(plugin._client.calls, ["12"])
        self.assertEqual(plugin.queries, [])
        self.assertTrue(any("Game" not in result.text and "12" in result.text for result in output))

    async def test_search_cover_failure_keeps_result_text(self):
        item = {"type": "game", "appid": 4090260, "name": "救生员狂热", "price": "$15.29", "image_url": "https://example.test/missing.jpg"}
        plugin = self.make_plugin([item])

        async def fail_download(url):
            raise steam_client.SteamAPIError("HTTP 404")

        plugin._client.download_bytes = fail_download
        output = await self.run_command(plugin, "/st 救生员狂热")
        self.assertEqual(len(output), 2)
        self.assertIn("找到 1 个", output[0].text)
        self.assertIn("救生员狂热（AppID 4090260）", output[1].text)
        self.assertEqual(output[1].image, None)

    async def test_search_cover_uses_local_bytes(self):
        item = {"type": "game", "appid": 4090260, "name": "救生员狂热", "price": "", "image_url": "https://example.test/cover.jpg"}
        plugin = self.make_plugin([item])
        output = await self.run_command(plugin, "/st 救生员狂热")
        self.assertIn("救生员狂热", output[1].text)
        self.assertTrue(output[1].image.startswith("base64://"))

    async def test_short_numeric_empty_result_falls_back_to_appid(self):
        plugin = self.make_plugin([], enhanced=True)
        output = await self.run_command(plugin, "/st 12")
        self.assertEqual(plugin._client.calls, ["12"])
        self.assertEqual(plugin.queries, [12])
        self.assertIn("Game 12", output[0].text)
        self.assertEqual(output[0].image, "base64://" + base64.b64encode(b"\xff\xd8\xfftest-cover").decode())

    async def test_four_digits_go_directly_to_appid(self):
        plugin = self.make_plugin([])
        output = await self.run_command(plugin, "/steam 1234")
        self.assertEqual(plugin._client.calls, [])
        self.assertEqual(plugin.queries, [1234])
        self.assertIn("Game 1234", output[0].text)

    async def test_mixed_content_searches_name(self):
        plugin = self.make_plugin([])
        await self.run_command(plugin, "/steam 12A")
        self.assertEqual(plugin._client.calls, ["12A"])
        self.assertEqual(plugin.queries, [])

    async def test_name_ending_with_language_word_is_not_truncated(self):
        plugin = self.make_plugin([])
        await self.run_command(plugin, "/steam A Game english")
        self.assertEqual(plugin._client.calls, ["A Game english"])
        self.assertEqual(plugin.queries, [])

    async def test_search_language_stays_schinese_in_another_region(self):
        item = {"type": "game", "appid": 123, "name": "中文游戏", "price": "", "image_url": ""}
        plugin = self.make_plugin([item], enhanced=True)
        plugin.config.update({"default_cc": "us", "default_lang": "english"})
        await self.run_command(plugin, "/steam 中文游戏")
        self.assertEqual(plugin._client.params, [("中文游戏", "us", "schinese")])
        self.assertEqual(plugin.queries, [])

    async def test_ai_fallback_only_after_short_name_and_appid_miss(self):
        plugin = self.make_plugin([], enhanced=True)
        order = []

        async def search(keyword, cc, lang):
            order.append("name")
            return []

        async def query(appid, cc, lang, **kwargs):
            order.append("appid")
            return models.SteamGameInfo(error=f"AppID {appid} 不存在或在当前地区不可见"), cc

        class ITAD:
            async def search_games(self, keyword, limit):
                order.append("itad")
                return []

        plugin._client.search_suggest = search
        plugin._query_with_fallback = query
        plugin._itad_client = ITAD()
        await self.run_command(plugin, "/steam 12")
        self.assertEqual(order, ["name", "appid", "itad"])

    async def test_long_appid_miss_tries_name_before_ai(self):
        plugin = self.make_plugin([], enhanced=True)
        order = []

        async def search(keyword, cc, lang):
            order.append("name")
            return []

        async def query(appid, cc, lang, **kwargs):
            order.append("appid")
            return models.SteamGameInfo(error=f"AppID {appid} 不存在或在当前地区不可见"), cc

        class ITAD:
            async def search_games(self, keyword, limit):
                order.append("itad")
                return []

        plugin._client.search_suggest = search
        plugin._query_with_fallback = query
        plugin._itad_client = ITAD()
        await self.run_command(plugin, "/steam 1234")
        self.assertEqual(order, ["appid", "name", "itad"])

    async def test_search_network_error_does_not_trigger_ai(self):
        plugin = self.make_plugin(plugin_module.SteamAPIError("请求超时"), enhanced=True)
        output = await self.run_command(plugin, "/steam Hades")
        self.assertIn("搜索失败", output[0].text)
        self.assertEqual(plugin.queries, [])

    async def test_appid_network_error_does_not_trigger_ai(self):
        plugin = self.make_plugin([], enhanced=True)

        async def timeout(appid, cc, lang, **kwargs):
            return models.SteamGameInfo(error="请求超时，请稍后重试"), cc

        plugin._query_with_fallback = timeout
        output = await self.run_command(plugin, "/steam 1234")
        self.assertIn("请求超时", output[0].text)
        self.assertEqual(plugin._client.calls, [])

    async def test_review_language_survives_appid_then_ai_fallback(self):
        plugin = self.make_plugin([], enhanced=True)
        plugin._session_review_lang["test:GroupMessage:1"] = "english"
        query_languages = []

        async def query(appid, cc, lang, **kwargs):
            query_languages.append(kwargs.get("review_lang"))
            if appid == 1234:
                return models.SteamGameInfo(error="AppID 1234 不存在或在当前地区不可见"), cc
            return models.SteamGameInfo(steam_appid=appid, name="Recovered"), cc

        class ITAD:
            async def search_games(self, keyword, limit):
                return [{"appid": 888, "title": "Recovered"}]

        class Service:
            async def get_game_info(self, appid, cc, lang, **kwargs):
                return models.SteamGameInfo(steam_appid=appid, name="Recovered")

        async def validate(keyword, results):
            return {"match_level": "high", "matched_indices": [0], "is_single_precise": True}

        plugin._query_with_fallback = query
        plugin._itad_client = ITAD()
        plugin._service = Service()
        plugin._llm_validate_search = validate
        output = await self.run_command(plugin, "/steam 1234")
        self.assertEqual(query_languages, ["english", "english"])
        self.assertIn("Recovered", output[0].text)


class SuggestParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_steam_html_with_no_type_class_is_not_discarded(self):
        html = (
            '<a class="match ds_collapse_flag " data-ds-appid="4090260">'
            '<img src="https://shared.fastly.steamstatic.com/apps/4090260/hashed/capsule_sm_120_schinese.jpg?t=1">'
            '<div class="match_name">救生员狂热</div>'
            '<div class="match_price">HK$ 48.60</div></a>'
            '<a class="match ds_collapse_flag " data-ds-appid="5009630">'
            '<img src="https://shared.fastly.steamstatic.com/apps/5009630/other.jpg">'
            '<div class="match_name">救生员狂热 - ArtBook</div>'
            '<div class="match_price">HK$ 15.39</div></a>'
        )

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def text(self):
                return html

        class Session:
            closed = False

            def get(self, *args, **kwargs):
                return Response()

        client = object.__new__(steam_client.SteamClient)
        client._session = Session()
        client._proxy = None

        async def no_rate_limit():
            pass

        client.check_query_rate_limit = no_rate_limit
        results = await client.search_suggest("救生员狂热", "hk", "schinese")
        self.assertEqual([item["appid"] for item in results], [4090260, 5009630])
        self.assertEqual([item["type"] for item in results], ["unknown", "unknown"])
        self.assertEqual(results[0]["name"], "救生员狂热")
        self.assertEqual(results[0]["image_url"], "https://shared.fastly.steamstatic.com/apps/4090260/hashed/capsule_sm_120_schinese.jpg?t=1")
        self.assertEqual(results[1]["image_url"], "https://shared.fastly.steamstatic.com/apps/5009630/other.jpg")

    async def test_unknown_candidates_are_checked_against_appdetails_and_cached(self):
        plugin = object.__new__(plugin_module.SteamStoreSniperPlugin)
        plugin.config = {"default_cc": "hk"}
        seen = []

        class Client:
            async def fetch_app_details(self, appid, cc, lang):
                seen.append((appid, cc, lang))
                return {"type": "game" if appid == 4090260 else "dlc"}

        plugin._client = Client()
        candidates = [
            {"appid": 4090260, "type": "unknown"},
            {"appid": 5009630, "type": "unknown"},
        ]
        first = await plugin._filter_search_games(candidates)
        second = await plugin._filter_search_games(candidates)
        self.assertEqual([item["appid"] for item in first], [4090260])
        self.assertEqual(first, second)
        self.assertCountEqual(seen, [(4090260, "hk", "schinese"), (5009630, "hk", "schinese")])

    async def test_type_lookup_error_keeps_candidate(self):
        plugin = object.__new__(plugin_module.SteamStoreSniperPlugin)
        plugin.config = {"default_cc": "hk"}

        class Client:
            async def fetch_app_details(self, appid, cc, lang):
                raise steam_client.SteamAPIError("请求超时")

        plugin._client = Client()
        candidates = [{"appid": 4090260, "type": "unknown"}]
        self.assertEqual(await plugin._filter_search_games(candidates), candidates)


class AdultScreenshotNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def screenshot_reply(self, required_age, descriptor_ids):
        plugin = object.__new__(plugin_module.SteamStoreSniperPlugin)
        plugin.config = {"default_cc": "hk", "default_lang": "schinese"}
        plugin._acl = _ACL()
        plugin._is_adult_blocked = lambda _origin: True

        async def query(appid, cc, lang, **kwargs):
            self.assertEqual(appid, 4242290)
            return models.SteamGameInfo(
                name="测试游戏",
                required_age=required_age,
                content_descriptor_ids=descriptor_ids,
                screenshots=[],
            ), cc

        plugin._query_with_fallback = query
        return [result async for result in plugin.cmd_steam_shots(_Event("/sts 4242290"))][0].text

    async def test_descriptor_only_does_not_claim_steam_age_zero(self):
        message = await self.screenshot_reply(0, [1, 3, 4])
        self.assertIn("按 18+ 规则屏蔽", message)
        self.assertNotIn("年龄限制 0+", message)

    async def test_explicit_age_keeps_steam_age_label(self):
        message = await self.screenshot_reply(18, [])
        self.assertIn("年龄限制 18+", message)

    async def test_unmarked_game_is_not_blocked(self):
        message = await self.screenshot_reply(0, [])
        self.assertIn("暂无截图数据", message)
        self.assertNotIn("被标记为成人内容", message)


class FormatTests(unittest.TestCase):
    def test_search_modes(self):
        self.assertEqual(router.search_mode("7"), "short_numeric")
        self.assertEqual(router.search_mode("123"), "short_numeric")
        self.assertEqual(router.search_mode("1234"), "id")
        self.assertEqual(router.search_mode("12 A"), "name")
        self.assertEqual(router.search_mode("１２"), "name")

    def test_simple_preset_has_only_requested_fields(self):
        game = models.SteamGameInfo(
            steam_appid=730, name="Example", short_description="Hidden description",
            header_image="https://example.test/cover.jpg",
            price_overview=models.PriceOverview(final_formatted="$5"),
            history_low_price=2.5, history_low_currency="USD",
            review_score_desc="特别好评", review_total_positive=9,
            review_total_reviews=10, review_lang="schinese",
        )
        text, image = formatter.format_game_info(game, "us", preset="simple")
        self.assertEqual(image, game.header_image)
        self.assertIn("$5", text)
        self.assertIn("史低 USD 2.50", text)
        self.assertIn("90% 好评", text)
        self.assertNotIn("Hidden description", text)
        self.assertNotIn("store.steampowered.com", text)

    def test_simple_preset_reports_missing_history_and_reviews(self):
        text, _ = formatter.format_game_info(models.SteamGameInfo(name="Example"), "hk", preset="simple")
        self.assertIn("史低：暂无数据", text)
        self.assertIn("评价：暂无数据", text)


if __name__ == "__main__":
    unittest.main()
