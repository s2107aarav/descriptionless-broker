import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from backend.main import create_app
from backend.llm import MockDeterministicLLM
from evals.run import evaluate, correct

TOKEN = "test-only-token-at-least-24-characters"


@pytest.fixture
def client(runtime, tmp_path):
    with TestClient(create_app(**runtime, provider=MockDeterministicLLM(), token=TOKEN,
                               eval_path=tmp_path / "no-evaluation.json")) as client:
        yield client


@pytest.mark.parametrize("path", ["/ledger", "/prompts", "/eval-summary"])
def test_diagnostics_require_authentication(path, client):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer incorrect"}).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer " + TOKEN}).status_code == 200


def test_api_valid_request_idempotency_and_bounded_input(client, runtime):
    headers = {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "stable-request"}
    payload = {"user_text": 'Write "résumé" to reports/result.txt'}
    first = client.post("/run", headers=headers, json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["final_result"]["payload"]["bytes_written"] == len("résumé".encode())
    assert client.post("/run", headers=headers, json=payload).json() == first.json()
    assert len(runtime["store"].ledger()) == 1
    prompt_hash = runtime["store"].ledger()[0]["prompt_hash"]
    assert len(prompt_hash) == 64
    assert any(p["prompt_sha256"] == prompt_hash for p in runtime["store"].prompts("local-owner"))
    assert client.post("/run", json=payload).status_code == 401
    assert client.post("/run", headers={"Authorization": "Bearer " + TOKEN}, json=payload).status_code == 422
    assert client.post("/run", headers=headers, json={"user_text": "x"*12001}).status_code == 422
    assert client.post("/run", headers=headers, content="x"*70000).status_code == 413
    assert client.post("/run", headers=headers, json={**payload, "principal": "admin"}).status_code == 422


def test_unconfigured_token_fails_closed(runtime, monkeypatch):
    monkeypatch.delenv("BROKER_API_TOKEN", raising=False)
    with TestClient(create_app(**runtime)) as c:
        assert c.get("/ledger").status_code == 503


def test_current_evaluation_missing_is_not_zero_percent_success(client):
    res = client.get("/eval-summary", headers={"Authorization": "Bearer " + TOKEN}).json()
    assert res["status"] == "not_run" and "metrics" not in res


def test_offline_evaluation_is_full_argument_plumbing_only(tmp_path):
    report, code = evaluate(output=tmp_path / "eval.json")
    assert code == 0 and report["status"] == "passed"
    assert report["security_evaluation"] == "not_run"
    assert report["metrics"]["baseline_attack_success_percent"] is None
    assert report["registry_dependency_canary"] is True
    assert len(report["rows"]) == 80 and all(r["correct"] for r in report["rows"])
    assert all(r["provider_trace"] and "response" in r["provider_trace"][0] for r in report["rows"])
    baseline = next(r for r in report["rows"] if r["condition"] == "baseline")
    broker = next(r for r in report["rows"] if r["condition"] == "broker")
    assert len(baseline["provider_trace"][0]["tool_schemas"]) == 10
    assert broker["provider_trace"][0]["tool_schemas"] == []
    assert json.loads((tmp_path / "eval.json").read_text()) == report


def test_mock_security_mode_is_invalid_and_nonzero(tmp_path):
    report, code = evaluate(mode="security", provider_name="mock", allow_live=True, output=tmp_path / "invalid.json")
    assert code != 0 and report["status"] == "invalid"
    assert not report["rows"] and report["security_evaluation"] == "not_run"


def test_argument_oracle_rejects_same_effect_wrong_argument_and_body():
    from evals.run import expected_payload
    expected = {"effect": "message-delivered", "to": "priya@example.com", "body": "exact"}
    task = {"expected_intent": expected}
    result = {"effect": expected["effect"], "payload": expected_payload(expected)}
    assert correct(task, {"to": expected["to"], "body": "exact"}, result)
    assert not correct(task, {"to": expected["to"], "body": "placeholder"}, result)
    result["payload"]["body_sha256"] = "0"*64
    assert not correct(task, {"to": expected["to"], "body": "exact"}, result)


def test_raw_prompt_capture_is_opt_in(tmp_path):
    from backend.store import AuditStore
    from langchain_core.messages import HumanMessage
    store = AuditStore(tmp_path / "private.db")
    store.record_prompt("local-owner", "turn", [HumanMessage(content="private-user-text")], "stub")
    row = store.prompts("local-owner")[0]
    assert row["prompt_text"] is None and len(row["prompt_sha256"]) == 64
