"""bot/gemini.py 的測試: request 組裝、response 解析、錯誤回報。不打網路。

call() 的 HTTP 路徑用假 aiohttp 驗 —— 錯誤訊息是 /summary-check 的產品本體,
「失敗時說得出原因」跟「成功時回得出摘要」一樣重要。
"""

import asyncio
import json
import sys
import types
import unittest
import unittest.mock
from contextlib import contextmanager

import _paths  # noqa: F401

import gemini
from gemini import build_request, parse_response, response_issue


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, resp=None, raise_exc=None):
        self._resp, self._raise = resp, raise_exc
        self.calls = []

    def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if self._raise:
            raise self._raise
        return self._resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@contextmanager
def fake_aiohttp(status=200, body="", raise_exc=None):
    """把假的 aiohttp 塞進 sys.modules — gemini.call 是延遲 import 的, 所以攔得到。"""
    mod = types.ModuleType("aiohttp")
    session = _FakeSession(_FakeResp(status, body), raise_exc)
    mod.ClientTimeout = lambda total=None: None
    mod.ClientSession = lambda timeout=None: session
    old = sys.modules.get("aiohttp")
    sys.modules["aiohttp"] = mod
    try:
        yield session
    finally:
        if old is None:
            sys.modules.pop("aiohttp", None)
        else:
            sys.modules["aiohttp"] = old


class TestBuildRequest(unittest.TestCase):
    def test_url_and_payload(self):
        url, payload = build_request("gemini-2.5-flash", "摘要這段")
        self.assertIn("gemini-2.5-flash:generateContent", url)
        self.assertEqual(payload["contents"][0]["parts"][0]["text"], "摘要這段")

    def test_default_model_when_blank(self):
        url, _ = build_request("", "x")
        self.assertIn(gemini.DEFAULT_MODEL, url)

    def test_api_key_never_in_url(self):
        # key 走 x-goog-api-key header, 不進 query string (避免被 proxy/log 記下來)
        url, payload = build_request("m", "x")
        self.assertNotIn("key=", url)
        self.assertNotIn("key", payload)


class TestParseResponse(unittest.TestCase):
    def _resp(self, parts):
        return {"candidates": [{"content": {"parts": parts}}]}

    def test_extracts_text(self):
        self.assertEqual(parse_response(self._resp([{"text": "摘要好了"}])), "摘要好了")

    def test_joins_multiple_parts(self):
        got = parse_response(self._resp([{"text": "第一段"}, {"text": "第二段"}]))
        self.assertEqual(got, "第一段\n第二段")

    def test_blocked_or_empty_returns_none(self):
        self.assertIsNone(parse_response({}))                      # 空物件
        self.assertIsNone(parse_response({"candidates": []}))       # 被擋掉
        self.assertIsNone(parse_response(self._resp([])))           # 沒有 parts
        self.assertIsNone(parse_response(self._resp([{"text": "  "}])))  # 只有空白
        self.assertIsNone(parse_response({"candidates": [{}]}))      # 沒有 content

    def test_garbage_shapes_tolerated(self):
        for bad in (None, "字串", 123, {"candidates": "nope"},
                    {"candidates": [{"content": {"parts": "nope"}}]}):
            self.assertIsNone(parse_response(bad))


class TestResponseIssue(unittest.TestCase):
    def test_block_reason(self):
        got = response_issue({"promptFeedback": {"blockReason": "SAFETY"}})
        self.assertIn("SAFETY", got)
        self.assertIn("被擋", got)

    def test_finish_reason(self):
        got = response_issue({"candidates": [{"finishReason": "MAX_TOKENS"}]})
        self.assertIn("MAX_TOKENS", got)

    def test_stop_is_not_reported_as_issue(self):
        # finishReason=STOP 是正常結束; 沒文字是別的原因, 不該誤導成「生成中止」
        got = response_issue({"candidates": [{"finishReason": "STOP"}]})
        self.assertNotIn("STOP", got)

    def test_api_error_object(self):
        got = response_issue({"error": {"status": "INVALID_ARGUMENT",
                                        "message": "API key not valid"}})
        self.assertIn("INVALID_ARGUMENT", got)
        self.assertIn("API key not valid", got)

    def test_no_candidates(self):
        self.assertIn("candidates", response_issue({"candidates": []}))

    def test_garbage(self):
        self.assertIn("格式", response_issue("字串"))


class TestCall(unittest.TestCase):
    """call() 要「回報」錯誤而不是吞掉 —— /summary-check 靠這個講出原因。"""

    def _call(self, **kw):
        return asyncio.run(gemini.call("prompt", api_key="k", model="m", **kw))

    def test_success(self):
        body = json.dumps({"candidates": [{"content": {"parts": [{"text": "摘要好了"}]}}]})
        with fake_aiohttp(200, body) as session:
            text, err = self._call()
        self.assertEqual(text, "摘要好了")
        self.assertIsNone(err)
        self.assertEqual(session.calls[0]["headers"]["x-goog-api-key"], "k")

    def test_no_api_key_reports_reason_without_network(self):
        text, err = asyncio.run(gemini.call("p", api_key=""))
        self.assertIsNone(text)
        self.assertIn("GEMINI_API_KEY", err)

    def test_http_error_reports_status_and_body(self):
        with fake_aiohttp(400, '{"error":{"message":"API key not valid"}}'):
            text, err = self._call()
        self.assertIsNone(text)
        self.assertIn("HTTP 400", err)
        self.assertIn("API key not valid", err)  # 內文要帶上, 不然使用者不知道怎麼修

    def test_non_json_body_reported(self):
        with fake_aiohttp(200, "<html>502 Bad Gateway</html>"):
            text, err = self._call()
        self.assertIsNone(text)
        self.assertIn("不是合法 JSON", err)
        self.assertIn("502", err)

    def test_blocked_response_reports_reason(self):
        with fake_aiohttp(200, json.dumps({"promptFeedback": {"blockReason": "SAFETY"}})):
            text, err = self._call()
        self.assertIsNone(text)
        self.assertIn("SAFETY", err)

    def test_network_exception_reports_type(self):
        with fake_aiohttp(raise_exc=TimeoutError("timed out")):
            text, err = self._call()
        self.assertIsNone(text)
        self.assertIn("TimeoutError", err)  # 型別名稱對診斷有用

    def test_long_error_body_truncated(self):
        with fake_aiohttp(500, "x" * 5000):
            text, err = self._call()
        self.assertIsNone(text)
        self.assertLess(len(err), 400)  # 要能貼進 Discord


class TestParseModels(unittest.TestCase):
    def test_default_chain_when_blank(self):
        for blank in (None, "", "   ", ",, ,"):
            self.assertEqual(gemini.parse_models(blank), gemini.DEFAULT_MODELS)

    def test_single_model(self):
        self.assertEqual(gemini.parse_models("gemini-2.5-flash"), ["gemini-2.5-flash"])

    def test_comma_and_whitespace_separated(self):
        want = ["a", "b", "c"]
        for raw in ("a,b,c", "a, b, c", "a b c", "a,  b   c", " a , b,c "):
            self.assertEqual(gemini.parse_models(raw), want)

    def test_order_is_preserved(self):
        # 順序就是嘗試順序, 不能排序或去重打亂它
        self.assertEqual(gemini.parse_models("z,a,m"), ["z", "a", "m"])

    def test_default_chain_shape(self):
        self.assertEqual(gemini.DEFAULT_MODEL, gemini.DEFAULT_MODELS[0])
        self.assertEqual(gemini.DEFAULT_MODELS,
                         ["gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gemma-4-31B"])


class TestCallChain(unittest.TestCase):
    """依序退到下一個 model。用假的 call() 驗鏈本身的行為。"""

    @contextmanager
    def _calls(self, outcomes):
        """outcomes: {model: (text, err)}; 沒列到的 model 一律當成 404。"""
        seen = []

        async def fake_call(prompt, *, api_key, model):
            seen.append(model)
            return outcomes.get(model, (None, "HTTP 404 — model not found"))

        with unittest.mock.patch.object(gemini, "call", fake_call):
            yield seen

    def _chain(self, models):
        return asyncio.run(gemini.call_chain("p", api_key="k", models=models))

    def test_first_model_wins_and_stops(self):
        with self._calls({"a": ("摘要", None)}) as seen:
            text, model, errors = self._chain(["a", "b", "c"])
        self.assertEqual((text, model, errors), ("摘要", "a", []))
        self.assertEqual(seen, ["a"])  # 成功就不該再試後面的

    def test_falls_through_to_next(self):
        with self._calls({"b": ("摘要", None)}) as seen:
            text, model, errors = self._chain(["a", "b", "c"])
        self.assertEqual(text, "摘要")
        self.assertEqual(model, "b")
        self.assertEqual(seen, ["a", "b"])
        self.assertEqual([m for m, _ in errors], ["a"])  # 前面失敗的要帶回去
        self.assertIn("404", errors[0][1])

    def test_all_fail_returns_every_error(self):
        with self._calls({}) as seen:
            text, model, errors = self._chain(["a", "b", "c"])
        self.assertIsNone(text)
        self.assertIsNone(model)
        self.assertEqual(seen, ["a", "b", "c"])
        self.assertEqual([m for m, _ in errors], ["a", "b", "c"])

    def test_no_api_key_short_circuits(self):
        # 每個 model 都會回同一句, 沒必要試 N 次
        with self._calls({"a": ("摘要", None)}) as seen:
            text, model, errors = asyncio.run(
                gemini.call_chain("p", api_key="", models=["a", "b"]))
        self.assertIsNone(text)
        self.assertEqual(seen, [])
        self.assertIn("GEMINI_API_KEY", errors[0][1])

    def test_defaults_used_when_models_omitted(self):
        with self._calls({gemini.DEFAULT_MODELS[0]: ("摘要", None)}) as seen:
            text, model, _ = asyncio.run(gemini.call_chain("p", api_key="k"))
        self.assertEqual(model, gemini.DEFAULT_MODELS[0])
        self.assertEqual(seen, [gemini.DEFAULT_MODELS[0]])

    def test_real_http_error_flows_through_chain(self):
        # 不用假 call, 走真的 call() -> 假 aiohttp: 404 也要能往後退
        with fake_aiohttp(404, '{"error":{"message":"model not found"}}'):
            text, model, errors = self._chain(["nope-1", "nope-2"])
        self.assertIsNone(model)
        self.assertEqual(len(errors), 2)
        self.assertIn("HTTP 404", errors[0][1])


if __name__ == "__main__":
    unittest.main()
