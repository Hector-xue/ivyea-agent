"""领星签名黄金向量 —— 逐字节锁死 make_sign 与权威实现一致。

向量用合成 16 字节 appId（与真实凭据无关），固定 params，期望值由权威算法
（ivyea-ops lingxing_openapi.make_sign）于 2026-06-17 生成并冻结。
"""
from __future__ import annotations

from ivyea_agent.lingxing_openapi import make_sign

APPID = "abcdef0123456789"  # 16 字节 = AES-128 key
PARAMS = {
    "access_token": "tok123", "app_key": APPID, "timestamp": 1700000000,
    "sid": 1876, "report_date": "2026-06-14", "length": 50, "offset": 0,
    "empty": "", "flag": True, "none_val": None, "arr": [3, None, 1], "obj": {"b": 2, "a": None},
}
GOLDEN = "UWlps6iR78/hOMhwyXGpS7JcBLXc0zEc8R3BMXpXo+zkm9UO/IkI5J3EKI3ROJxs"


def test_sign_golden_vector():
    assert make_sign(PARAMS, APPID) == GOLDEN


def test_sign_skips_empty_keeps_zero():
    # offset=0 必须保留（0 != ""），empty="" 必须跳过 —— 否则签名会变
    s_with_zero = make_sign({"a": 0, "b": "x"}, APPID)
    s_no_a = make_sign({"b": "x"}, APPID)
    assert s_with_zero != s_no_a
    s_with_empty = make_sign({"a": "", "b": "x"}, APPID)
    assert s_with_empty == s_no_a  # 空串等价于不存在


def test_sign_bool_lowercased():
    assert make_sign({"f": True}, APPID) == make_sign({"f": "true"}, APPID)
