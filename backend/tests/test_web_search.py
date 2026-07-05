"""web_search 可插拔后端回归 (SearXNG → ddgs → DDG 爬虫兜底)。

离线: monkeypatch _resolve_backend / _http_get / _search_ddgs, 绝不真实外呼。
锚定: 后端解析、SearXNG/HTML 解析、主后端失败回退爬虫、egress 审计、注册/沙箱。
"""
from __future__ import annotations

import unittest

try:
    from ._helpers import fresh_conn
except ImportError:
    from _helpers import fresh_conn

from mozi_backend.config import Settings  # noqa: E402
from mozi_backend.skills import tools  # noqa: E402

_DDG_HTML = """
<div><a class="result__a" href="u1">今日<b>热点</b>一</a>
<a class="result__snippet">摘要一 &amp; 内容</a></div>
<div><a class="result__a" href="u2">头条二</a>
<a class="result__snippet">摘要二</a></div>
"""
_SEARXNG_JSON = '{"results":[{"title":"墨子-百科","content":"兼爱非攻是墨家核心","url":"http://x/1"},' \
                '{"title":"墨子思想","content":"尚贤节用","url":"http://x/2"}]}'


class BackendResolveTest(unittest.TestCase):
    def test_searxng_when_url_set(self) -> None:
        orig = tools.settings
        try:
            tools.settings = Settings(searxng_url="http://searxng:8080")
            self.assertEqual(tools._resolve_backend(), "searxng")
        finally:
            tools.settings = orig

    def test_ddgs_when_installed_and_no_searxng(self) -> None:
        orig = tools.settings
        try:
            tools.settings = Settings(searxng_url=None)
            # ddgs 已在 requirements 中安装 → 默认后端
            self.assertEqual(tools._resolve_backend(), "ddgs")
        finally:
            tools.settings = orig


class ParseTest(unittest.TestCase):
    def test_searxng_json_parse(self) -> None:
        orig_get, orig_set = tools._http_get, tools.settings
        try:
            tools.settings = Settings(searxng_url="http://searxng:8080")
            tools._http_get = lambda url, params, headers: _SEARXNG_JSON
            rows = tools._search_searxng("墨子")
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["title"], "墨子-百科")
            self.assertEqual(rows[0]["snippet"], "兼爱非攻是墨家核心")
            self.assertEqual(rows[0]["url"], "http://x/1")
        finally:
            tools._http_get, tools.settings = orig_get, orig_set

    def test_ddg_html_parse(self) -> None:
        orig = tools._http_get
        try:
            tools._http_get = lambda url, params, headers: _DDG_HTML
            rows = tools._search_ddg_html("x")
            self.assertEqual([r["title"] for r in rows], ["今日热点一", "头条二"])
            self.assertEqual(rows[0]["snippet"], "摘要一 & 内容")
        finally:
            tools._http_get = orig


class WebSearchDispatchTest(unittest.TestCase):
    def test_registered_auto_sandbox(self) -> None:
        self.assertIn("web_search", tools.TOOLS)
        self.assertIn("web_search", tools.AUTO_TOOLS)
        self.assertEqual(tools.plan_tools(["web_search"]), ["web_search"])
        self.assertFalse(tools.enforce_allowed("web_search", []))

    def test_primary_failure_falls_back_to_html(self) -> None:
        orig_res, orig_ddgs, orig_get = tools._resolve_backend, tools._search_ddgs, tools._http_get
        try:
            tools._resolve_backend = lambda: "ddgs"

            def _boom(q):
                raise RuntimeError("ddgs down")

            tools._search_ddgs = _boom
            tools._http_get = lambda url, params, headers: _DDG_HTML
            conn = fresh_conn()
            try:
                res = tools._web_search(conn, "u_demo", "今天热点")
                self.assertEqual(res["count"], 2, "主后端失败 → 回退爬虫仍出结果")
                self.assertIn("->ddg_html", res["backend"], "backend 标注回退路径")
                # egress 审计 (外呼受控点)
                row = conn.execute("SELECT egress_flag FROM audit_log "
                                   "WHERE action='tool.web_search'").fetchone()
                self.assertEqual(row["egress_flag"], 1)
            finally:
                conn.close()
        finally:
            tools._resolve_backend, tools._search_ddgs, tools._http_get = orig_res, orig_ddgs, orig_get

    def test_total_failure_degrades_empty(self) -> None:
        orig_res, orig_html = tools._resolve_backend, tools._search_ddg_html
        try:
            tools._resolve_backend = lambda: "ddg_html"

            def _boom(q):
                raise RuntimeError("network down")

            tools._search_ddg_html = _boom
            conn = fresh_conn()
            try:
                res = tools._web_search(conn, "u_demo", "x")
                self.assertEqual(res["count"], 0, "全失败 → 空结果不崩")
                self.assertIn("fail", res["backend"])
                self.assertIsNotNone(conn.execute(
                    "SELECT 1 FROM audit_log WHERE action='tool.web_search'").fetchone())
            finally:
                conn.close()
        finally:
            tools._resolve_backend, tools._search_ddg_html = orig_res, orig_html

    def test_sovereign_no_searxng_blocks_no_egress(self) -> None:
        """主权硬门: sovereign 且无 SEARXNG_URL → 拦截且绝不外呼 (HIGH#security)。"""
        orig_settings, orig_get = tools.settings, tools._http_get
        try:
            tools.settings = Settings(searxng_url=None)

            def _no_net(*a, **k):
                raise AssertionError("sovereign 拦截下不应发起任何网络请求")

            tools._http_get = _no_net
            conn = fresh_conn()
            try:
                res = tools._web_search(conn, "u_demo", "热点", privacy_tier="sovereign")
                self.assertEqual(res["backend"], "blocked")
                self.assertEqual(res["error"], "egress_blocked")
                self.assertEqual(res["count"], 0)
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM audit_log WHERE action='tool.web_search'").fetchone())
            finally:
                conn.close()
        finally:
            tools.settings, tools._http_get = orig_settings, orig_get


if __name__ == "__main__":
    unittest.main()
