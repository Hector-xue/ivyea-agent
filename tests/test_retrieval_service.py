from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest


def test_retrieval_combines_knowledge_and_memory(ivyea_home):
    from ivyea_agent import memory, retrieval

    memory.remember("Prime Day 预算要保护品牌词，不要误否核心词。", asin="B0LOCAL")
    result = retrieval.search("预算 品牌词", limit=8)

    assert result["mode"] == "local_hybrid_lexical_vector"
    assert any(h["source"] == "knowledge" for h in result["hits"])
    assert any(h["source"] == "memory" for h in result["hits"])
    caps = result["capabilities"]
    assert caps["local"] is True
    assert caps["local_vectors"]["enabled"] is True
    assert caps["index"]["backend"] == "local_hash_embedding_v1"
    assert caps["semantic_vectors"]["enabled"] is False


def test_retrieval_index_rebuild_and_status(ivyea_home):
    from ivyea_agent import retrieval, retrieval_index

    rebuilt = retrieval.rebuild_index()
    assert rebuilt["ok"] is True
    assert rebuilt["chunks"] > 0
    assert rebuilt["knowledge_cards"] >= 1

    status = retrieval_index.status()
    assert status["enabled"] is True
    assert status["backend"] == "local_hash_embedding_v1"
    assert status["chunks"] == rebuilt["chunks"]

    hits = retrieval_index.search("主图 转化", limit=3)
    assert hits
    assert hits[0]["source"] == "knowledge_index"
    assert hits[0]["match"] == "local_hash_embedding_v1"


def test_retrieval_embeddings_status_and_config(ivyea_home):
    from ivyea_agent import retrieval

    default = retrieval.embeddings_status()
    assert default["configured_backend"] == "hash"
    assert default["active_backend"] == "local_hash_embedding_v1"
    assert default["semantic_enabled"] is False
    assert default["offline_safe"] is True

    semantic = retrieval.configure_embeddings(
        backend="sentence-transformers",
        model="BAAI/bge-small-zh-v1.5",
        model_path="",
        allow_download=False,
    )
    assert semantic["configured_backend"] == "sentence-transformers"
    assert semantic["semantic_enabled"] is False
    assert semantic["active_backend"] == "local_hash_embedding_v1"
    assert semantic["fallback_reason"]
    probe = retrieval.probe_embeddings()
    assert probe["ready"] is True
    assert probe["active_backend"] == "local_hash_embedding_v1"

    rebuilt = retrieval.rebuild_index()
    assert rebuilt["backend"] == "local_hash_embedding_v1"
    assert rebuilt["embeddings"]["configured_backend"] == "sentence-transformers"


def test_embedding_encode_falls_back_when_dense_model_fails(ivyea_home, monkeypatch):
    from ivyea_agent import retrieval_embeddings

    monkeypatch.setattr(retrieval_embeddings, "status", lambda: {
        "semantic_enabled": True,
        "model": "broken-model",
        "model_path": "",
        "active_backend": "sentence-transformers",
    })

    def boom(text, st):
        raise RuntimeError("model load failed")

    monkeypatch.setattr(retrieval_embeddings, "_sentence_vector", boom)
    encoded = retrieval_embeddings.encode_query("否词")
    assert encoded["kind"] == "sparse"
    assert encoded["backend"] == "local_hash_embedding_v1"
    assert "model load failed" in encoded["fallback_error"]

    probe = retrieval_embeddings.probe()
    assert probe["ready"] is False
    assert probe["active_backend"] == "local_hash_embedding_v1"


def test_retrieval_cli_outputs_json(ivyea_home, capsys):
    from ivyea_agent.cli import main

    assert main(["retrieval", "embeddings", "--json"]) == 0
    emb = json.loads(capsys.readouterr().out)
    assert emb["embeddings"]["active_backend"] == "local_hash_embedding_v1"

    assert main(["retrieval", "embeddings", "--backend", "sentence-transformers", "--no-download", "--json"]) == 0
    emb = json.loads(capsys.readouterr().out)
    assert emb["embeddings"]["configured_backend"] == "sentence-transformers"
    assert emb["embeddings"]["active_backend"] == "local_hash_embedding_v1"

    assert main(["retrieval", "embeddings", "--probe", "--json"]) == 0
    emb = json.loads(capsys.readouterr().out)
    assert "probe" in emb
    assert emb["probe"]["active_backend"] == "local_hash_embedding_v1"

    assert main(["retrieval", "index", "--json"]) == 0
    indexed = json.loads(capsys.readouterr().out)
    assert indexed["ok"] is True
    assert indexed["chunks"] > 0

    assert main(["retrieval", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["index"]["backend"] == "local_hash_embedding_v1"

    assert main(["retrieval", "search", "否词", "--limit", "3", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["query"] == "否词"
    assert data["hits"]

    assert main(["retrieval", "capabilities"]) == 0
    out = capsys.readouterr().out
    assert "Ivyea 本地检索能力" in out


def test_serve_cli_rejects_remote_bind_by_default(ivyea_home, capsys):
    from ivyea_agent.cli import main

    assert main(["serve", "--host", "0.0.0.0"]) == 2
    err = capsys.readouterr().err
    assert "--allow-remote" in err


def test_serve_cli_requires_token_for_remote_bind(ivyea_home, capsys, monkeypatch):
    from ivyea_agent import service
    from ivyea_agent.cli import main

    called = {}
    monkeypatch.setattr(service, "run", lambda **kw: called.update(kw))

    assert main(["serve", "--host", "0.0.0.0", "--allow-remote"]) == 2
    assert "API token" in capsys.readouterr().err

    assert main(["serve", "--host", "0.0.0.0", "--allow-remote", "--api-token", "secret"]) == 0
    assert called["api_token"] == "secret"


def test_service_health_shape_without_socket(ivyea_home):
    from ivyea_agent import service

    data = service.health()
    assert data["ok"] is True
    assert data["version"]
    assert data["retrieval"]["local"] is True
    assert "key_status" in data["model"]

    manifest = service.manifest()
    assert manifest["api_version"] == "v1"
    assert manifest["security"]["secrets_in_responses"] is False
    assert manifest["capabilities"]["chat"] is True
    assert manifest["capabilities"]["knowledge_management"] is True
    assert manifest["capabilities"]["workspace_understanding"] is True
    assert manifest["capabilities"]["code_agent"] is True
    assert manifest["capabilities"]["write_execution"] is False
    assert any(e["path"] == "/v1/openapi.json" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/system/status" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/system/doctor" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/chat" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/chat/stream" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/chat/sessions" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/chat/sessions/{id}" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/skills" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/skills/search" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/knowledge/cards" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/knowledge/audit" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/knowledge/conflicts" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/knowledge/rebuild" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/knowledge/cards/{id}" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/retrieval/embeddings" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/retrieval/embeddings/probe" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/retrieval/status" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/retrieval/search" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/retrieval/index" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/tasks" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/traces" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/traces/stats" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/workspace/index" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/workspace/search" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/workspace/impact" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/code/plan" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/code/context" for e in manifest["endpoints"])
    assert any(e["path"] == "/v1/code/repair" for e in manifest["endpoints"])

    spec = service.openapi_spec()
    assert spec["openapi"] == "3.1.0"
    assert spec["info"]["title"] == "Ivyea Agent Local API"
    assert "/v1/chat" in spec["paths"]
    assert "/v1/chat/stream" in spec["paths"]
    assert "/v1/skills/search" in spec["paths"]
    assert "/v1/knowledge/audit" in spec["paths"]
    assert "/v1/knowledge/rebuild" in spec["paths"]
    assert "/v1/workspace/index" in spec["paths"]
    assert "/v1/code/plan" in spec["paths"]
    assert "/v1/traces/stats" in spec["paths"]


def test_service_task_api_helpers(ivyea_home):
    from ivyea_agent import service

    created = service.task_create({"title": "Embed task", "steps": ["inspect", "patch"], "notes": "local"})
    task_id = created["task"]["id"]
    assert created["task"]["status"] == "pending"

    listed = service.task_list(limit=5)
    assert any(t["id"] == task_id for t in listed["tasks"])

    started = service.task_update(task_id, "start", {"notes": "begin"})
    assert started["task"]["status"] == "in_progress"

    stepped = service.task_update(task_id, "step", {"index": 1, "status": "completed", "notes": "done"})
    assert stepped["task"]["steps"][0]["status"] == "completed"

    logged = service.task_update(task_id, "log", {"text": "visible in IvyeaOps"})
    assert logged["task"]["events"][-1]["text"] == "visible in IvyeaOps"

    detail = service.task_detail(task_id)
    assert detail["task"]["id"] == task_id


def test_service_trace_api_helpers(ivyea_home):
    from ivyea_agent import service, traces

    traces.record("svc-session", "turn-1", "tool_call", "knowledge_search", ok=True, duration_ms=8, summary="ok")
    traces.record("other-session", "turn-1", "tool_call", "run_patrol", ok=False, duration_ms=20, summary="failed")

    listed = service.trace_list(limit=10, session_id="svc-session")
    assert listed["ok"] is True
    assert len(listed["traces"]) == 1
    assert listed["traces"][0]["name"] == "knowledge_search"

    stats = service.trace_stats(limit=10)
    assert stats["stats"]["tool_calls"] == 2
    assert stats["stats"]["failures"] == 1


def test_service_system_helpers(ivyea_home):
    from ivyea_agent import service

    status = service.system_status()
    assert status["ok"] is True
    assert status["status"]["version"]

    doctor = service.system_doctor()
    assert "checks" in doctor
    assert any(check["name"] == "retrieval embeddings" for check in doctor["checks"])


def test_service_skill_and_knowledge_helpers(ivyea_home):
    from ivyea_agent import service

    sks = service.skill_list(limit=20)
    assert sks["ok"] is True
    assert any(row["id"] == "amazon.search_term_optimizer" for row in sks["skills"])

    found = service.skill_search("否词", limit=5)
    assert found["skills"]
    detail = service.skill_detail(found["skills"][0]["id"])
    assert detail["skill"]["body"]
    assert "linked_knowledge" in detail["skill"]

    cards = service.knowledge_cards(limit=20)
    assert cards["cards"]
    assert "body" not in cards["cards"][0]

    card = service.knowledge_detail("amazon_ads.sponsored_products_targeting")
    assert card["card"]["body"]
    assert card["card"]["license"] == "amazon_public_docs_summary"

    created = service.knowledge_create({
        "id": "user.service-note",
        "title": "服务导入知识",
        "body": "预算爆掉时先看 placement 和搜索词质量，避免直接扩大泛词。",
        "tags": ["budget", "placement"],
    })
    assert created["ok"] is True
    assert created["card"]["id"] == "user.service-note"
    assert created["indexes"]["knowledge"]["cards"] >= 1

    audit = service.knowledge_audit()
    assert audit["ok"] is True
    assert audit["summary"]["user_cards"] >= 1
    assert any(row["id"] == "user.service-note" for row in audit["cards"])

    conflicts = service.knowledge_conflicts()
    assert conflicts["ok"] is True
    assert "conflicts" in conflicts

    rebuilt = service.knowledge_rebuild()
    assert rebuilt["ok"] is True
    assert rebuilt["index"]["cards"] >= 1
    assert rebuilt["retrieval_index"]["chunks"] >= 1


class _ServiceChatProvider:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        self.calls += 1
        assert messages[-1]["role"] == "user"
        assert "Ivyea 本地知识检索" in messages[-1]["content"]
        return {"role": "assistant", "content": "只读分析完成", "tool_calls": []}


class _ServiceToolProvider:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "name": "knowledge_search", "arguments": {"query": "否词", "limit": 1}}],
            }
        return {"role": "assistant", "content": "已结合知识库回答", "tool_calls": []}


class _ServiceSessionProvider:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        self.calls += 1
        if self.calls == 2:
            assert any("第一轮" in str(m.get("content") or "") for m in messages)
        return {"role": "assistant", "content": f"第{self.calls}轮回答", "tool_calls": []}


class _ServiceStreamProvider:
    def stream_chat(self, messages, tools=None, temperature=0.3, timeout=120.0):
        yield {"type": "text", "text": "流式"}
        yield {"type": "text", "text": "完成"}
        yield {"type": "final", "content": "流式完成", "tool_calls": [], "usage": {"prompt_tokens": 3}}


def test_service_chat_run_with_fake_provider(ivyea_home):
    from ivyea_agent import service

    result = service.chat_run({"message": "主图转化怎么判断", "max_steps": 2}, provider=_ServiceChatProvider())

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["text"] == "只读分析完成"
    assert result["messages"][-1]["role"] == "assistant"
    assert result["session_id"]


def test_service_chat_run_with_tool_event(ivyea_home):
    from ivyea_agent import service

    result = service.chat_run({"message": "否词规则", "max_steps": 3}, provider=_ServiceToolProvider())

    assert result["ok"] is True
    assert result["text"] == "已结合知识库回答"
    assert any("knowledge_search" in e["text"] for e in result["events"])
    assert any(m["role"] == "tool" for m in result["messages"])


def test_service_chat_stream_with_fake_provider(ivyea_home):
    from ivyea_agent import service

    events = []
    result = service.chat_stream(
        {"message": "流式测试", "inject_retrieval": False, "max_steps": 2},
        lambda event, data: events.append((event, data)),
        provider=_ServiceStreamProvider(),
    )

    assert result["ok"] is True
    assert result["text"] == "流式完成"
    assert events[0][0] == "start"
    assert [e for e, _ in events].count("token") >= 2
    assert events[-1][0] == "final"


def test_service_chat_sessions_persist_and_resume(ivyea_home):
    from ivyea_agent import service

    created = service.chat_session_create({"title": "运营对话"})
    sid = created["session"]["id"]
    provider = _ServiceSessionProvider()

    first = service.chat_run({"session_id": sid, "message": "第一轮", "inject_retrieval": False}, provider=provider)
    assert first["ok"] is True
    assert first["session_id"] == sid

    second = service.chat_run({"session_id": sid, "message": "第二轮", "inject_retrieval": False}, provider=provider)
    assert second["ok"] is True
    assert second["text"] == "第2轮回答"

    detail = service.chat_session_detail(sid)
    assert detail["session"]["id"] == sid
    assert len([m for m in detail["session"]["messages"] if m["role"] == "assistant"]) == 2

    listed = service.chat_session_list(limit=5)
    assert any(row["id"] == sid for row in listed["sessions"])


def test_local_service_health_and_retrieval(ivyea_home, monkeypatch):
    from ivyea_agent import service

    try:
        server = service.make_server("127.0.0.1", 0)
    except PermissionError:
        pytest.skip("local socket binding is not available in this sandbox")
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        assert health["ok"] is True
        assert health["name"] == "ivyea-agent"
        assert health["retrieval"]["local"] is True

        with urllib.request.urlopen(f"http://{host}:{port}/v1/openapi.json", timeout=5) as resp:
            spec = json.loads(resp.read().decode("utf-8"))
        assert spec["openapi"] == "3.1.0"
        assert "/v1/chat/stream" in spec["paths"]

        payload = json.dumps({"query": "高点击 零单 否词", "limit": 3}).encode("utf-8")
        req = urllib.request.Request(
            f"http://{host}:{port}/v1/retrieval/search",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        assert result["ok"] is True
        assert result["hits"]

        monkeypatch.setattr(service, "chat_run", lambda body: {"ok": True, "text": body.get("message"), "events": []})
        req = urllib.request.Request(
            f"http://{host}:{port}/v1/chat",
            data=json.dumps({"message": "hello"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            chat = json.loads(resp.read().decode("utf-8"))
        assert chat["ok"] is True
        assert chat["text"] == "hello"

        monkeypatch.setattr(service, "chat_stream", lambda body, send: send("final", {"ok": True, "text": "stream"}))
        req = urllib.request.Request(
            f"http://{host}:{port}/v1/chat/stream",
            data=json.dumps({"message": "hello"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            ctype = resp.headers.get("Content-Type", "")
        assert "text/event-stream" in ctype
        assert "event: final" in body

        with urllib.request.urlopen(f"http://{host}:{port}/v1/system/status", timeout=5) as resp:
            system_status = json.loads(resp.read().decode("utf-8"))
        assert system_status["ok"] is True
        assert system_status["status"]["version"]

        with urllib.request.urlopen(f"http://{host}:{port}/v1/system/doctor", timeout=5) as resp:
            system_doctor = json.loads(resp.read().decode("utf-8"))
        assert "checks" in system_doctor

        req = urllib.request.Request(
            f"http://{host}:{port}/v1/chat/sessions",
            data=json.dumps({"title": "http session"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            created = json.loads(resp.read().decode("utf-8"))
        assert created["ok"] is True
        sid = created["session"]["id"]

        with urllib.request.urlopen(f"http://{host}:{port}/v1/chat/sessions", timeout=5) as resp:
            listed = json.loads(resp.read().decode("utf-8"))
        assert any(row["id"] == sid for row in listed["sessions"])

        with urllib.request.urlopen(f"http://{host}:{port}/v1/chat/sessions/{sid}", timeout=5) as resp:
            detail = json.loads(resp.read().decode("utf-8"))
        assert detail["ok"] is True
        assert detail["session"]["id"] == sid

        with urllib.request.urlopen(f"http://{host}:{port}/v1/skills/search?q=%E5%90%A6%E8%AF%8D", timeout=5) as resp:
            skill_hits = json.loads(resp.read().decode("utf-8"))
        assert skill_hits["ok"] is True
        assert skill_hits["skills"]

        with urllib.request.urlopen(f"http://{host}:{port}/v1/knowledge/cards/amazon_ads.sponsored_products_targeting", timeout=5) as resp:
            card = json.loads(resp.read().decode("utf-8"))
        assert card["ok"] is True
        assert card["card"]["body"]

        req = urllib.request.Request(
            f"http://{host}:{port}/v1/knowledge/cards",
            data=json.dumps({
                "id": "user.http-note",
                "title": "HTTP 导入知识",
                "body": "高点击零单要先拆语义和承接，不要直接否核心词。",
                "tags": ["negative", "listing"],
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            imported = json.loads(resp.read().decode("utf-8"))
        assert imported["ok"] is True
        assert imported["card"]["id"] == "user.http-note"

        with urllib.request.urlopen(f"http://{host}:{port}/v1/knowledge/audit", timeout=5) as resp:
            audit = json.loads(resp.read().decode("utf-8"))
        assert audit["ok"] is True
        assert any(row["id"] == "user.http-note" for row in audit["cards"])

        with urllib.request.urlopen(f"http://{host}:{port}/v1/knowledge/conflicts", timeout=5) as resp:
            conflicts = json.loads(resp.read().decode("utf-8"))
        assert conflicts["ok"] is True
        assert "conflicts" in conflicts

        req = urllib.request.Request(
            f"http://{host}:{port}/v1/knowledge/rebuild",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            rebuilt = json.loads(resp.read().decode("utf-8"))
        assert rebuilt["ok"] is True
        assert rebuilt["index"]["cards"] >= 1

        from ivyea_agent import traces
        traces.record("http-session", "turn-1", "tool_call", "knowledge_search", summary="ok")
        with urllib.request.urlopen(f"http://{host}:{port}/v1/traces?session_id=http-session", timeout=5) as resp:
            trace_rows = json.loads(resp.read().decode("utf-8"))
        assert trace_rows["ok"] is True
        assert trace_rows["traces"][0]["session_id"] == "http-session"

        with urllib.request.urlopen(f"http://{host}:{port}/v1/traces/stats", timeout=5) as resp:
            trace_stats = json.loads(resp.read().decode("utf-8"))
        assert trace_stats["ok"] is True
        assert trace_stats["stats"]["events"] >= 1

        req = urllib.request.Request(
            f"http://{host}:{port}/v1/retrieval/index",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            indexed = json.loads(resp.read().decode("utf-8"))
        assert indexed["ok"] is True
        assert indexed["chunks"] > 0

        with urllib.request.urlopen(f"http://{host}:{port}/v1/retrieval/embeddings", timeout=5) as resp:
            embeddings = json.loads(resp.read().decode("utf-8"))
        assert embeddings["ok"] is True
        assert embeddings["embeddings"]["active_backend"]

        req = urllib.request.Request(
            f"http://{host}:{port}/v1/retrieval/embeddings/probe",
            data=json.dumps({"text": "probe"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            probe = json.loads(resp.read().decode("utf-8"))
        assert probe["ok"] is True
        assert probe["probe"]["active_backend"]

        req = urllib.request.Request(
            f"http://{host}:{port}/v1/retrieval/embeddings",
            data=json.dumps({"backend": "hash"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            configured = json.loads(resp.read().decode("utf-8"))
        assert configured["ok"] is True
        assert configured["embeddings"]["configured_backend"] == "hash"

        with urllib.request.urlopen(f"http://{host}:{port}/v1/retrieval/status", timeout=5) as resp:
            status = json.loads(resp.read().decode("utf-8"))
        assert status["ok"] is True
        assert status["index"]["enabled"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_local_service_token_auth(ivyea_home):
    from ivyea_agent import service

    try:
        server = service.make_server("127.0.0.1", 0, api_token="secret")
    except PermissionError:
        pytest.skip("local socket binding is not available in this sandbox")
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            urllib.request.urlopen(f"http://{host}:{port}/health", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("unauthorized request should fail")

        req = urllib.request.Request(
            f"http://{host}:{port}/health",
            headers={"Authorization": "Bearer secret"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        assert health["ok"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
