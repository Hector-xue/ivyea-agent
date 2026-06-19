"""Structured operating profiles."""
from __future__ import annotations


def test_update_get_resolve_profile(ivyea_home):
    from ivyea_agent import profiles

    default = profiles.update(
        "default",
        site="us",
        target_acos=0.28,
        margin_rate=0.34,
        price=29.99,
        protected_terms="Ivyea, karaoke machine",
        core_terms="karaoke, singing machine",
        listing_risks="review弱,主图不清晰",
    )
    assert default["site"] == "US"
    assert default["target_acos"] == 0.28
    assert default["margin_rate"] == 0.34
    assert default["price"] == 29.99
    assert default["protected_terms"] == ["Ivyea", "karaoke machine"]
    assert default["listing_risks"] == ["review弱", "主图不清晰"]

    profiles.update("B0ABCDEF12", target_acos=0.22, stage="launch")
    resolved = profiles.resolve(asin="B0ABCDEF12")
    assert resolved["site"] == "US"
    assert resolved["target_acos"] == 0.22
    assert resolved["stage"] == "launch"
    assert "Ivyea" in resolved["protected_terms"]


def test_context_text_and_list(ivyea_home):
    from ivyea_agent import profiles

    profiles.update("sid:1876", target_acos=0.25, protected_terms=["Brand"])
    rows = dict(profiles.list_profiles())
    assert "default" in rows
    assert "sid:1876" in rows

    text = profiles.context_text(profiles.resolve(store="sid:1876"), label="sid:1876")
    assert "运营画像:sid:1876" in text
    assert "目标 ACOS: 25%" in text
    assert "保护词: Brand" in text


def test_cli_profile_set_show(ivyea_home, capsys):
    from ivyea_agent.cli import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "profile", "set", "B0ABCDEF12",
        "--target-acos", "0.21",
        "--margin-rate", "0.35",
        "--breakeven-acos", "0.35",
        "--price", "39.99",
        "--protected", "Ivyea,Brand",
        "--stage", "growth",
        "--listing-risks", "主图弱,评论少",
    ])
    assert args.func(args) == 0

    args = parser.parse_args(["profile", "show", "B0ABCDEF12"])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "目标 ACOS: 21%" in out
    assert "毛利率: 35%" in out
    assert "盈亏平衡 ACOS: 35%" in out
    assert "价格: USD 39.99" in out
    assert "保护词: Ivyea, Brand" in out
    assert "Listing 风险: 主图弱, 评论少" in out
    assert "生命周期阶段: growth" in out
