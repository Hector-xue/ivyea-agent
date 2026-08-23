"""亚马逊官方 API 的凭据与授权（P7a）。

一套凭据喂两个 API：SP-API（库存/订单/Listing/价格）与 Ads API（广告）。
它们共用同一个 LWA（Login with Amazon）应用和同一个 refresh_token，
只是换 token 时的 scope 与请求头不同 —— 所以换 token 这件事收在这里一处。

**契约来源（2026-08-23 逐条核实，不是凭记忆写的）**：

- LWA：``POST https://api.amazon.com/auth/o2/token``，表单参数
  ``grant_type=refresh_token`` / ``refresh_token`` / ``client_id`` / ``client_secret``
  —— developer-docs.amazon「Connecting to the Selling Partner API」。
- SP-API 访问令牌走 ``x-amz-access-token`` 请求头（**不是** Authorization）。
- SP-API 区域主机：``sellingpartnerapi-na|eu|fe.amazon.com``（同上文档「SP-API endpoints」）。
- Ads API 主机 ``advertising-api[-eu|-fe].amazon.com``，令牌走 ``Authorization: Bearer``，
  另需 ``Amazon-Advertising-API-ClientId`` 与 ``Amazon-Advertising-API-Scope``
  —— amzn/ads-advanced-tools-docs 官方 Postman 集合。

**为什么在没有账号的情况下也先写**：用这套系统的人有真实账号。凭据能填、
数据源能接、规则能吃到官方数据，这三件事不该等到某一台机器恰好有账号才开始做。
契约有公开且权威的文档，那就照文档写、用 fixtures 测；真账号到位后跑
``ivyea amazon verify`` 即可端到端验收。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

from . import config

_TOKEN_FILE = config.IVYEA_DIR / "amazon_token.json"
_LOCK = threading.Lock()

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

#: 提前 5 分钟刷新，避免边界上用到刚过期的 token（与飞书那套同一做法）
_REFRESH_MARGIN = 300.0

SPAPI_HOSTS = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}
ADS_HOSTS = {
    "na": "https://advertising-api.amazon.com",
    "eu": "https://advertising-api-eu.amazon.com",
    "fe": "https://advertising-api-fe.amazon.com",
}

ENV_CLIENT_ID = "AMAZON_LWA_CLIENT_ID"
ENV_CLIENT_SECRET = "AMAZON_LWA_CLIENT_SECRET"
ENV_REFRESH_TOKEN = "AMAZON_LWA_REFRESH_TOKEN"
#: 广告 API 允许用另一套 LWA 应用（很多卖家的广告应用和 SP-API 应用是分开审批的）。
#: 留空则与 SP-API 共用上面那套。
ENV_ADS_CLIENT_ID = "AMAZON_ADS_CLIENT_ID"
ENV_ADS_CLIENT_SECRET = "AMAZON_ADS_CLIENT_SECRET"
ENV_ADS_REFRESH_TOKEN = "AMAZON_ADS_REFRESH_TOKEN"

#: 常用站点 → 区域。用户只填站点，区域自动推 —— 让人去背
#: "英国属于 eu 还是 na" 是没必要的一步。
MARKETPLACE_REGION = {
    # 北美
    "ATVPDKIKX0DER": ("na", "US"), "A2EUQ1WTGCTBG2": ("na", "CA"),
    "A1AM78C64UM0Y8": ("na", "MX"), "A2Q3Y263D00KWC": ("na", "BR"),
    # 欧洲 + 中东 + 印度
    "A1F83G8C2ARO7P": ("eu", "UK"), "A1PA6795UKMFR9": ("eu", "DE"),
    "A13V1IB3VIYZZH": ("eu", "FR"), "APJ6JRA9NG5V4": ("eu", "IT"),
    "A1RKKUPIHCS9HS": ("eu", "ES"), "A1805IZSGTT6HS": ("eu", "NL"),
    "A2NODRKZP88ZB9": ("eu", "SE"), "A1C3SOZRARQ6R3": ("eu", "PL"),
    "AMEN7PMS3EDWL": ("eu", "BE"), "A17E79C6D8DWNP": ("eu", "SA"),
    "A2VIGQ35RCS4UG": ("eu", "AE"), "ARBP9OOSHTCHU": ("eu", "EG"),
    "A33AVAJ2PDY3EV": ("eu", "TR"), "A21TJRUUN4KGV": ("eu", "IN"),
    # 远东
    "A1VC38T7YXB528": ("fe", "JP"), "A39IBJ37TRP1C6": ("fe", "AU"),
    "A19VAU5U5O7RUS": ("fe", "SG"),
}


class AmazonAuthError(Exception):
    """凭据缺失或换 token 失败。**消息里绝不带 secret。**"""


def _settings() -> dict[str, Any]:
    return config.load_settings().get("amazon") or {}


def region() -> str:
    """区域。显式配置优先；没配就按第一个站点推。"""
    s = _settings()
    explicit = str(s.get("region") or "").strip().lower()
    if explicit in SPAPI_HOSTS:
        return explicit
    for m in s.get("marketplaces") or []:
        hit = MARKETPLACE_REGION.get(str(m.get("marketplace_id") or ""))
        if hit:
            return hit[0]
    return "na"


def creds(*, ads: bool = False) -> tuple[str, str, str]:
    """(client_id, client_secret, refresh_token)。

    广告那套留空时**回落到 SP-API 的**：绝大多数卖家两边用同一个应用，
    强迫他们把同一串东西填两遍只会填错一遍。
    """
    import os
    config.load_env()
    if ads:
        cid = os.environ.get(ENV_ADS_CLIENT_ID, "")
        sec = os.environ.get(ENV_ADS_CLIENT_SECRET, "")
        ref = os.environ.get(ENV_ADS_REFRESH_TOKEN, "")
        if cid and sec and ref:
            return cid, sec, ref
    return (os.environ.get(ENV_CLIENT_ID, ""),
            os.environ.get(ENV_CLIENT_SECRET, ""),
            os.environ.get(ENV_REFRESH_TOKEN, ""))


def is_configured(*, ads: bool = False) -> bool:
    return all(creds(ads=ads))


def marketplaces() -> list[dict[str, Any]]:
    """已配置的站点列表，每项 ``{sid, marketplace_id, name, ads_profile_id}``。

    ``sid`` 是与领星共用的**连接键**：同时用两边的人，同一个站点填同一个 sid，
    规则拿到的就是同一家店的数据（官方源优先，领星兜底）。只用亚马逊的人，
    sid 随便给一个稳定值即可（留空则用站点代码）。
    """
    out = []
    for m in _settings().get("marketplaces") or []:
        mid = str(m.get("marketplace_id") or "").strip()
        if not mid:
            continue
        reg, code = MARKETPLACE_REGION.get(mid, (region(), mid[:4]))
        out.append({
            "sid": str(m.get("sid") or code),
            "marketplace_id": mid,
            "name": str(m.get("name") or code),
            "country": code,
            "region": reg,
            "ads_profile_id": str(m.get("ads_profile_id") or "").strip(),
            "seller_id": str(m.get("seller_id") or _settings().get("seller_id") or "").strip(),
        })
    return out


def marketplace_for(sid: Any) -> Optional[dict[str, Any]]:
    key = str(sid)
    for m in marketplaces():
        if m["sid"] == key:
            return m
    return None


def spapi_host() -> str:
    return SPAPI_HOSTS.get(region(), SPAPI_HOSTS["na"])


def ads_host() -> str:
    return ADS_HOSTS.get(region(), ADS_HOSTS["na"])


# ── token ───────────────────────────────────────────────────────────────────
def _load_tokens() -> dict[str, Any]:
    if not _TOKEN_FILE.exists():
        return {}
    try:
        return json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_tokens(data: dict[str, Any]) -> None:
    import os
    config.ensure_dirs()
    _TOKEN_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(_TOKEN_FILE, 0o600)
    except OSError:
        pass


def access_token(*, ads: bool = False, force: bool = False) -> str:
    """换一个 LWA access token（1 小时有效），带落盘缓存。

    SP-API 与 Ads 分开缓存：即便用同一个应用，两边的 token 生命周期各自独立，
    共用一个槽位会互相把对方刷掉。
    """
    import httpx

    slot = "ads" if ads else "spapi"
    with _LOCK:
        store = _load_tokens()
        tok = store.get(slot) or {}
        if (not force and tok.get("token")
                and float(tok.get("expires_at") or 0) - _REFRESH_MARGIN > time.time()):
            return str(tok["token"])

        cid, secret, refresh = creds(ads=ads)
        if not (cid and secret and refresh):
            raise AmazonAuthError(
                "未配置亚马逊 LWA 凭据（client_id / client_secret / refresh_token）")
        try:
            with httpx.Client(timeout=30.0) as client:
                r = client.post(LWA_TOKEN_URL, data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": cid,
                    "client_secret": secret,
                })
        except httpx.HTTPError as exc:
            raise AmazonAuthError(f"LWA 换 token 网络失败：{exc}") from exc
        try:
            data = r.json()
        except ValueError as exc:
            raise AmazonAuthError(f"LWA 响应不可解析（HTTP {r.status_code}）") from exc
        if r.status_code >= 400 or not data.get("access_token"):
            # 只回显 error/description，绝不回显请求体（里面有 secret）
            raise AmazonAuthError(
                f"LWA 换 token 失败：{data.get('error')} {data.get('error_description')}")

        store[slot] = {"token": data["access_token"],
                       "expires_at": time.time() + float(data.get("expires_in") or 3600)}
        _save_tokens(store)
        return str(store[slot]["token"])


def drop_tokens() -> None:
    """换凭据后立刻作废缓存 —— 留着它下一次请求就以旧应用的身份发出去。"""
    try:
        _TOKEN_FILE.unlink()
    except OSError:
        pass


def status() -> dict[str, Any]:
    """配置全景（不含任何密钥）。IvyeaOps 的配置页读它。"""
    import os
    config.load_env()
    mkts = marketplaces()
    return {
        "configured": is_configured(),
        "ads_configured": is_configured(ads=True),
        "ads_uses_own_app": bool(os.environ.get(ENV_ADS_CLIENT_ID)),
        "region": region(),
        "spapi_host": spapi_host(),
        "ads_host": ads_host(),
        "seller_id": str(_settings().get("seller_id") or ""),
        "marketplaces": mkts,
        "marketplace_count": len(mkts),
        "with_ads_profile": sum(1 for m in mkts if m["ads_profile_id"]),
        "token_cached": bool(_load_tokens()),
        # 站点目录给界面做下拉。让人手抄 "A1F83G8C2ARO7P" 是配置流程里最容易
        # 抄错的一步，而抄错的表现是"保存成功但一条数据都没有"。
        "catalog": [{"marketplace_id": mid, "country": code, "region": reg}
                    for mid, (reg, code) in sorted(
                        MARKETPLACE_REGION.items(), key=lambda kv: kv[1][::-1])],
    }


def configure(payload: dict[str, Any]) -> dict[str, Any]:
    """写入凭据与站点。与飞书那套同一套规矩（见 feishu_setup.configure）：

    - 键不在 payload 里 = 不动；
    - 字符串为空 = 不动（界面上没填的框会老实传空串，当成清除就会出现
      「打开配置页什么都没干、保存一下就瞎了」），要清除得在 ``clear`` 里点名；
    - ``marketplaces`` 给了就整体替换（它是一张表，逐项合并说不清"删掉一行"）。
    """
    import os

    clear = {str(x).strip() for x in (payload.get("clear") or [])}
    changed: list[str] = []
    settings = config.load_settings()
    amazon = dict(settings.get("amazon") or {})

    env_fields = {
        "client_id": ENV_CLIENT_ID, "client_secret": ENV_CLIENT_SECRET,
        "refresh_token": ENV_REFRESH_TOKEN,
        "ads_client_id": ENV_ADS_CLIENT_ID, "ads_client_secret": ENV_ADS_CLIENT_SECRET,
        "ads_refresh_token": ENV_ADS_REFRESH_TOKEN,
    }
    touched_creds = False
    for field, env in env_fields.items():
        if field not in payload and field not in clear:
            continue
        val = str(payload.get(field) or "").strip()
        if not val and field not in clear:
            continue
        config.set_env_key(env, val)
        # 同步当前进程：serve 是常驻的，load_env() 又明确不覆盖已有环境变量，
        # 只写文件会出现"界面显示新凭据、发出去的还是旧应用"。
        if val:
            os.environ[env] = val
        else:
            os.environ.pop(env, None)
        changed.append(field)
        touched_creds = True

    if "region" in payload:
        val = str(payload.get("region") or "").strip().lower()
        if val in SPAPI_HOSTS:
            amazon["region"] = val
            changed.append("region")
    if "seller_id" in payload:
        val = str(payload.get("seller_id") or "").strip()
        if val or "seller_id" in clear:
            amazon["seller_id"] = val
            changed.append("seller_id")
    if "marketplaces" in payload:
        rows = []
        for m in payload.get("marketplaces") or []:
            mid = str(m.get("marketplace_id") or "").strip()
            if not mid:
                continue
            rows.append({
                "sid": str(m.get("sid") or "").strip(),
                "marketplace_id": mid,
                "name": str(m.get("name") or "").strip(),
                "ads_profile_id": str(m.get("ads_profile_id") or "").strip(),
                "seller_id": str(m.get("seller_id") or "").strip(),
            })
        amazon["marketplaces"] = rows
        changed.append("marketplaces")

    settings["amazon"] = amazon
    config.save_settings(settings)
    if touched_creds:
        drop_tokens()
    return {"ok": True, "changed": changed, "status": status()}
