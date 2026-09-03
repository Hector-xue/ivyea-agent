"""网页来源要能点开原文。

用户看完一份网页调研的回答说"末尾的引用来源还是不能直接点击跳转"——那份来源清单
八条里一个 URL 都没有，而地址一直在模型手上（就在它自己那次 web_fetch 的入参里）。
所以把地址放回**结果这一侧**、紧挨着内容，并直说引用的写法。
"""
from __future__ import annotations


class _Resp:
    def __init__(self, text, status=200, ct="text/html"):
        self.text, self.status_code, self.headers = text, status, {"content-type": ct}


def test_web_fetch_hands_the_url_back_with_the_citation_rule(monkeypatch):
    import httpx
    from ivyea_agent import tools_general

    url = "https://ccaf101.com/fde/salary"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp("<p>FDE 薪资分档</p>"))
    out = tools_general.t_web_fetch({"url": url}, None)

    assert "FDE 薪资分档" in out                     # 正文照旧
    assert f"[来源] {url}" in out                    # 地址跟着内容一起回到模型手上
    assert f"]({url})" in out                        # 且直说要写成 markdown 链接


def test_failed_fetch_carries_no_source_footer(monkeypatch):
    """403/521 没有"原文"可引 —— 给个地址只会让模型去引一页空的。"""
    import httpx
    from ivyea_agent import tools_general

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp("", status=403))
    assert tools_general.t_web_fetch({"url": "https://x.example/a"}, None) == "HTTP 403"


def test_web_search_results_keep_their_urls_and_the_rule(monkeypatch):
    from ivyea_agent import tools_general

    monkeypatch.setattr(tools_general, "_search_results",
                        lambda q, n=8: [("FDE 薪资报告", "https://www.fdehub.cc/report")])
    out = tools_general.t_web_search({"query": "FDE 薪资"}, None)

    assert "https://www.fdehub.cc/report" in out
    assert "markdown 链接" in out


def test_empty_search_says_so_without_the_rule(monkeypatch):
    from ivyea_agent import tools_general

    monkeypatch.setattr(tools_general, "_search_results", lambda q, n=8: [])
    assert tools_general.t_web_search({"query": "x"}, None) == "（无结果，或搜索源受限）"
