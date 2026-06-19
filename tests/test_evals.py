from __future__ import annotations


def test_product_evals_pass():
    from ivyea_agent import evals

    result = evals.run()
    assert result["ok"], evals.render(result)
    assert any(c["name"] == "skill.listing_recall" for c in result["checks"])


def test_eval_cli(capsys):
    from ivyea_agent.cli import main

    assert main(["eval"]) == 0
    out = capsys.readouterr().out
    assert "Ivyea Agent Eval" in out
    assert "result: PASS" in out
