from __future__ import annotations

import json
import threading
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
    assert caps["semantic_vectors"]["enabled"] is False


def test_retrieval_cli_outputs_json(ivyea_home, capsys):
    from ivyea_agent.cli import main

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


def test_service_health_shape_without_socket(ivyea_home):
    from ivyea_agent import service

    data = service.health()
    assert data["ok"] is True
    assert data["version"]
    assert data["retrieval"]["local"] is True
    assert "key_status" in data["model"]


def test_local_service_health_and_retrieval(ivyea_home):
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
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
