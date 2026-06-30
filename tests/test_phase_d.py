"""Phase D：help 分组 + 别名（不删命令）。"""
from __future__ import annotations

import re

from ivyea_agent import cli


def _strip(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def test_aliases_map_to_canonical():
    assert cli._SLASH_ALIASES["/h"] == "/help"
    assert cli._SLASH_ALIASES["/q"] == "/exit"


def test_help_lists_every_slash_command():
    txt = _strip(cli._help_text())
    for cmd, _desc in cli.SLASH_COMMANDS:
        assert cmd in txt, f"/help 漏了 {cmd}"


def test_help_has_group_titles():
    txt = _strip(cli._help_text())
    for title, _ in cli._SLASH_GROUPS:
        assert title in txt


def test_cli_epilog_groups_present():
    p = cli.build_parser()
    assert "广告运营" in (p.epilog or "") and "代码 / 工程" in (p.epilog or "")
