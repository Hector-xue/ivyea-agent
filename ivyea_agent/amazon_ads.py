"""Amazon Ads API 客户端（P7a）—— 活动配置与 v3 异步报表。

**契约来源**：amzn/ads-advanced-tools-docs 的官方 Postman 集合（2026-08-23 核实）。
三个容易踩空的点，都在这里防住：

1. **三个请求头缺一不可**：``Authorization: Bearer``、
   ``Amazon-Advertising-API-ClientId``、``Amazon-Advertising-API-Scope``（profileId）。
   少了 Scope 会拿到 401，而错误信息不会说"你少了 Scope"。
2. **版本化的媒体类型**：``POST /sp/campaigns/list`` 的 Accept 与 Content-Type 都得是
   ``application/vnd.spCampaign.v3+json``。用普通 ``application/json`` 会被拒。
3. **报表是异步的**：``POST /reporting/reports`` 只拿到 reportId，
   要轮询 ``GET /reporting/reports/{id}`` 直到 ``status=COMPLETED``，
   再去下载 ``url``（**GZIP_JSON**，且那个下载链接是预签名的、不要带 Ads 的头）。
"""
from __future__ import annotations

import gzip
import io
import json
import time
from typing import Any, Optional

from . import amazon_auth

SP_CAMPAIGN_MEDIA = "application/vnd.spCampaign.v3+json"
REPORT_MEDIA = "application/vnd.createasyncreportrequest.v3+json"

#: 报表生成通常几十秒到几分钟。给足 5 分钟，超时就报缺口 ——
#: 巡检不能为了一张报表挂在那里，别的店还等着跑。
REPORT_TIMEOUT = 300.0
REPORT_POLL_INTERVAL = 10.0


class AdsApiError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _headers(profile_id: str, media: str = "application/json") -> dict[str, str]:
    cid, _secret, _refresh = amazon_auth.creds(ads=True)
    return {
        "Authorization": f"Bearer {amazon_auth.access_token(ads=True)}",
        "Amazon-Advertising-API-ClientId": cid,
        "Amazon-Advertising-API-Scope": str(profile_id),
        "Accept": media,
        "Content-Type": media,
    }


def _call(method: str, path: str, profile_id: str, *,
          body: Optional[dict[str, Any]] = None, media: str = "application/json",
          timeout: float = 40.0) -> Any:
    import httpx

    url = amazon_ads_host() + path
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.request(method, url, headers=_headers(profile_id, media), json=body)
    except httpx.HTTPError as exc:
        raise AdsApiError(f"Ads API {path} 网络失败：{exc}") from exc
    if r.status_code >= 400:
        raise AdsApiError(f"Ads API {path} 失败（HTTP {r.status_code}）：{r.text[:200]}",
                          r.status_code)
    try:
        return r.json()
    except ValueError as exc:
        raise AdsApiError(f"Ads API {path} 响应不可解析（HTTP {r.status_code}）") from exc


def amazon_ads_host() -> str:
    return amazon_auth.ads_host()


def profiles() -> list[dict[str, Any]]:
    """广告账号档案。``GET /v2/profiles`` —— 这一个接口**不需要 Scope 头**
    （Scope 本身就是从这里拿的），所以单独走。"""
    import httpx

    cid, _s, _r = amazon_auth.creds(ads=True)
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(amazon_ads_host() + "/v2/profiles", headers={
                "Authorization": f"Bearer {amazon_auth.access_token(ads=True)}",
                "Amazon-Advertising-API-ClientId": cid,
            })
    except httpx.HTTPError as exc:
        raise AdsApiError(f"取广告档案失败：{exc}") from exc
    if r.status_code >= 400:
        raise AdsApiError(f"取广告档案失败（HTTP {r.status_code}）：{r.text[:200]}",
                          r.status_code)
    return list(r.json() or [])


def campaigns(profile_id: str, *, states: tuple[str, ...] = ("ENABLED", "PAUSED"),
              max_results: int = 500) -> list[dict[str, Any]]:
    """SP 活动清单。注意版本化媒体类型（见模块开头第 2 点）。"""
    data = _call("POST", "/sp/campaigns/list", profile_id,
                 body={"stateFilter": {"include": list(states)},
                       "maxResults": int(max_results)},
                 media=SP_CAMPAIGN_MEDIA)
    return list((data or {}).get("campaigns") or [])


def campaign_report(profile_id: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    """SP 活动日报表（v3 异步）。返回逐日逐活动的行。

    ``columns`` 里的 ``cost`` 就是花费（v3 改名了，不再叫 spend —— 照抄 v2 的
    字段名会拿到 400）；``purchases7d`` / ``sales7d`` 是 7 天归因口径，
    与领星报表的口径对齐，换源时规则读到的数不会突然变一截。
    """
    created = _call("POST", "/reporting/reports", profile_id, body={
        "name": f"ivyea sp campaigns {start_date}~{end_date}",
        "startDate": start_date,
        "endDate": end_date,
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": ["campaign"],
            "columns": ["date", "campaignId", "campaignName", "impressions",
                        "clicks", "cost", "purchases7d", "sales7d"],
            "reportTypeId": "spCampaigns",
            "timeUnit": "DAILY",
            "format": "GZIP_JSON",
        },
    }, media=REPORT_MEDIA)

    report_id = str((created or {}).get("reportId") or "")
    if not report_id:
        raise AdsApiError(f"创建报表未返回 reportId：{str(created)[:200]}")

    deadline = time.time() + REPORT_TIMEOUT
    url = ""
    while time.time() < deadline:
        info = _call("GET", f"/reporting/reports/{report_id}", profile_id, media=REPORT_MEDIA)
        status = str((info or {}).get("status") or "").upper()
        if status == "COMPLETED":
            url = str(info.get("url") or "")
            break
        if status in ("FAILURE", "FAILED", "CANCELLED"):
            raise AdsApiError(f"报表生成失败：{info.get('statusDetails') or status}")
        time.sleep(REPORT_POLL_INTERVAL)
    if not url:
        raise AdsApiError(f"报表 {report_id} 在 {REPORT_TIMEOUT:.0f} 秒内未完成")
    return _download_gzip_json(url)


def _download_gzip_json(url: str) -> list[dict[str, Any]]:
    """下载报表。**这个链接是预签名的，不能带 Ads 的鉴权头** ——
    带上去反而会被 S3 判成签名冲突。"""
    import httpx

    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            r = client.get(url)
    except httpx.HTTPError as exc:
        raise AdsApiError(f"下载报表失败：{exc}") from exc
    if r.status_code >= 400:
        raise AdsApiError(f"下载报表失败（HTTP {r.status_code}）")
    raw = r.content
    if raw[:2] == b"\x1f\x8b":                       # gzip 魔数
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AdsApiError("报表内容不是 JSON") from exc
    return list(data or [])
