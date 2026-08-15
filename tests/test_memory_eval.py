"""评测框架单测。

这个模块存在的意义就是"别再靠感觉调检索"，所以它自己的正确性尤其要钉死：
指标算错比没有指标更危险——会让人拿着错的数字下结论。
"""
from __future__ import annotations

import json


class FakeProvider:
    def __init__(self, queries):
        self.queries = queries
        self.calls = 0

    def complete(self, system, user, json_mode=False, temperature=0.2, timeout=60.0):
        self.calls += 1
        return json.dumps({"queries": self.queries}, ensure_ascii=False)


class BoomProvider:
    def complete(self, *a, **kw):
        raise RuntimeError("模型挂了")


def _seed(memory_store):
    memory_store.apply("add", name="备货节奏", category="domain",
                       description="补货时机与断货预防", content="周转天数低于30天就下单。")
    memory_store.apply("add", name="出价策略", category="domain",
                       description="竞价调整与花费控制", content="acos超标就降竞价。")


# ── 指标计算 ────────────────────────────────────────────────────────────────
def test_metrics_perfect_score(ivyea_home):
    from ivyea_agent import memory_eval
    m = memory_eval._metrics([1, 1, 1])
    assert m["recall@1"] == 1.0 and m["mrr"] == 1.0 and m["missed"] == 0


def test_metrics_all_missed(ivyea_home):
    from ivyea_agent import memory_eval
    m = memory_eval._metrics([None, None])
    assert m["recall@5"] == 0.0 and m["mrr"] == 0.0 and m["missed"] == 2


def test_recall_at_k_boundaries(ivyea_home):
    """排第 3 的必须算进 recall@3 而不算进 recall@1——边界错了整套数字都没意义。"""
    from ivyea_agent import memory_eval
    m = memory_eval._metrics([3])
    assert m["recall@1"] == 0.0 and m["recall@3"] == 1.0 and m["recall@5"] == 1.0


def test_mrr_rewards_higher_rank(ivyea_home):
    """同样命中，排第 1 必须比排第 5 得分高——这正是 recall@k 看不出的差别。"""
    from ivyea_agent import memory_eval
    assert memory_eval._metrics([1])["mrr"] > memory_eval._metrics([5])["mrr"]


def test_rank_of_matches_case_insensitively(ivyea_home):
    from ivyea_agent import memory_eval
    hits = [{"name": "别的"}, {"name": "备货节奏"}]
    assert memory_eval._rank_of(hits, ["备货节奏"]) == 2
    assert memory_eval._rank_of(hits, ["不存在"]) is None


def test_rank_of_any_expect_counts(ivyea_home):
    """expect 是列表时任一命中即算命中——同一个问题可能有多条合理答案。"""
    from ivyea_agent import memory_eval
    hits = [{"name": "出价策略"}]
    assert memory_eval._rank_of(hits, ["备货节奏", "出价策略"]) == 1


# ── 数据集读写 ──────────────────────────────────────────────────────────────
def test_dataset_roundtrip(ivyea_home):
    from ivyea_agent import memory_eval
    memory_eval.save_dataset([{"query": "问题", "expect": ["记忆"], "note": ""}])
    assert memory_eval.load_dataset()[0]["query"] == "问题"


def test_dataset_normalizes_scalar_expect(ivyea_home):
    """手写评测集时 expect 很容易写成字符串而不是列表，不能因此整条丢掉。"""
    from ivyea_agent import memory_eval
    memory_eval.save_dataset([{"query": "问题", "expect": "记忆"}])
    assert memory_eval.load_dataset()[0]["expect"] == ["记忆"]


def test_broken_dataset_does_not_crash(ivyea_home):
    from ivyea_agent import memory_eval
    p = memory_eval.dataset_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ 这不是合法 JSON", encoding="utf-8")
    assert memory_eval.load_dataset() == []


def test_incomplete_rows_skipped(ivyea_home):
    from ivyea_agent import memory_eval
    memory_eval.save_dataset([{"query": "有问题没答案"}, {"query": "好的", "expect": ["X"]}])
    assert len(memory_eval.load_dataset()) == 1


# ── 跑评测 ──────────────────────────────────────────────────────────────────
def test_run_scores_real_search(ivyea_home):
    from ivyea_agent import memory_eval, memory_store
    _seed(memory_store)
    memory_eval.save_dataset([
        {"query": "补货时机", "expect": ["备货节奏"]},
        {"query": "竞价调整", "expect": ["出价策略"]},
    ])
    res = memory_eval.run()
    assert res["ok"] and res["cases"] == 2
    assert res["recall@5"] == 1.0


def test_run_reports_misses(ivyea_home):
    from ivyea_agent import memory_eval, memory_store
    _seed(memory_store)
    memory_eval.save_dataset([{"query": "完全不相干的问题", "expect": ["备货节奏"]}])
    # 强制纯词法：自带语义默认开着，而向量召回在只有两条记忆的语料上必然把两条都返回，
    # "召不回"这件事就再也构造不出来了。这条测的是**记账逻辑**（miss 有没有算对），
    # 不是召回质量，所以把语义关掉才测得准。
    from ivyea_agent import memory_vectors
    with memory_vectors.lexical_only():
        res = memory_eval.run(semantic=False)
    assert res["missed"] == 1
    assert "完全不相干的问题" in memory_eval.render(res)


def test_run_empty_dataset(ivyea_home):
    from ivyea_agent import memory_eval
    assert not memory_eval.run()["ok"]


def test_lexical_only_context_manager_restores(ivyea_home):
    """漏还原会让后续所有查询静默降级——比报错更难查，必须测。"""
    from ivyea_agent import memory_vectors
    assert memory_vectors._FORCE_LEXICAL is False
    try:
        with memory_vectors.lexical_only():
            assert memory_vectors._FORCE_LEXICAL is True
            raise RuntimeError("模拟异常")
    except RuntimeError:
        pass
    assert memory_vectors._FORCE_LEXICAL is False


def test_compare_shape(ivyea_home):
    from ivyea_agent import memory_eval, memory_store
    _seed(memory_store)
    memory_eval.save_dataset([{"query": "补货时机", "expect": ["备货节奏"]}])
    res = memory_eval.compare()
    assert res["ok"] and "delta" in res
    assert set(res["lexical"]) >= {"recall@1", "mrr"}
    # 自带语义默认开着，所以 compare 现在两边都有意义；渲染里不该再挂"未启用"的提示
    assert res["semantic_available"] is True
    assert "未启用语义后端" not in memory_eval.render(res)


# ── 生成评测集 ──────────────────────────────────────────────────────────────
def test_generate_creates_cases(ivyea_home):
    from ivyea_agent import memory_eval, memory_store
    _seed(memory_store)
    res = memory_eval.generate(FakeProvider(["东西快没了", "什么时候补货", "断货怎么办"]))
    assert res["ok"] and res["added"] == 6        # 2 条记忆 × 3 个问题
    assert len(memory_eval.load_dataset()) == 6


def test_generate_skips_already_covered(ivyea_home):
    """反复 --generate 不能让用例翻倍。"""
    from ivyea_agent import memory_eval, memory_store
    _seed(memory_store)
    p = FakeProvider(["问题一"])
    memory_eval.generate(p)
    first_calls = p.calls
    res = memory_eval.generate(p)
    assert res["added"] == 0
    assert p.calls == first_calls              # 已覆盖的记忆一次模型都没调


def test_generate_survives_provider_failure(ivyea_home):
    from ivyea_agent import memory_eval, memory_store
    _seed(memory_store)
    res = memory_eval.generate(BoomProvider())
    assert res["ok"] and res["added"] == 0 and res["failed_entries"] == 2


def test_generate_without_memories(ivyea_home):
    from ivyea_agent import memory_eval
    assert not memory_eval.generate(FakeProvider(["x"]))["ok"]


def test_generated_cases_marked(ivyea_home):
    """手写用例通常来自真实翻车案例，比生成的值钱，要能区分开。"""
    from ivyea_agent import memory_eval, memory_store
    _seed(memory_store)
    memory_eval.save_dataset([{"query": "手写的", "expect": ["备货节奏"]}])
    memory_eval.generate(FakeProvider(["生成的"]))
    st = memory_eval.status()
    assert st["handwritten"] == 1 and st["generated"] >= 1


def test_status_shape(ivyea_home):
    from ivyea_agent import memory_eval
    st = memory_eval.status()
    assert st["cases"] == 0 and st["exists"] is False
