"""Boundary, failure-injection, concurrency and idempotency regression tests."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import pytest
from backend.intents import BaseIntent, SendMessage, ReadFile, WriteFile, LookupRecord, CreateTicket
from backend.broker import route_and_execute
from backend.policy import ExecutionPolicy
from backend.store import canonical_json_hash
from backend.registry import Registry
from backend import tools_mock


def test_unsupported_effect(runtime):
    class FakeIntent(BaseIntent):
        effect: str = "unknown-effect"
    assert route_and_execute(FakeIntent(), **runtime).payload == "unsupported-effect"


def test_invalid_schema_version(runtime):
    class BadVersionIntent(BaseIntent):
        schema_version: str = "v2"
        effect: str = "message-delivered"
    assert route_and_execute(BadVersionIntent(), **runtime).payload == "invalid-schema-version"


@pytest.mark.parametrize("intent", [
    SendMessage(to="test@example.com", body="Hello"),
    ReadFile(path="workspace/a.txt"), WriteFile(path="reports/a.txt", content="résumé"),
    LookupRecord(query="customer 41"), CreateTicket(title="Fix", detail="Login issue"),
])
def test_all_effects_have_bound_closed_receipts(intent, runtime):
    result = route_and_execute(intent, **runtime)
    assert result.effect == intent.effect
    assert not ({"handler_name", "provider", "endpoint", "tool_id"} & set(result.payload))
    ledger = runtime["store"].ledger()
    assert len(ledger) == 1 and ledger[0]["status"] == "completed"
    assert json.loads(ledger[0]["request_json"]) == intent.model_dump()
    assert json.loads(ledger[0]["result_json"]) == result.model_dump()
    assert ledger[0]["canonical_hash"] == canonical_json_hash(intent.model_dump())


def test_priority_then_id_and_unknown_payload_rejection(runtime):
    reg = runtime["registry"]
    fixtures = [
        {"tool_id": "tool-2", "effect": "message-delivered", "handler_name": "smtp_send_b", "description": "B", "priority": 10},
        {"tool_id": "tool-1", "effect": "message-delivered", "handler_name": "smtp_send_a", "description": "A", "priority": 10}]
    reg.load_fixtures(fixtures)
    assert route_and_execute(SendMessage(to="test@example.com", body="hi"), **runtime).effect == "message-delivered"
    assert runtime["store"].ledger()[0]["chosen_tool_id"] == "tool-1"
    fixtures[0]["priority"] = 1
    reg.load_fixtures(fixtures)
    assert route_and_execute(SendMessage(to="test@example.com", body="hi"), **runtime).payload == "execution-unknown"


@pytest.mark.parametrize("key", ["tool_id", "Tool_ID", "provider", "Provider", "debug_info", "anything"])
def test_payload_unknown_keys_fail_closed(key, runtime, monkeypatch):
    original = tools_mock.smtp_send_a
    def poisoned(**kwargs):
        raw = original(**kwargs)
        raw["payload"][key] = {"safe_val": "ok", "secret": "untrusted"}
        return raw
    monkeypatch.setattr(tools_mock, "smtp_send_a", poisoned)
    result = route_and_execute(SendMessage(to="test@example.com", body="hi"), **runtime)
    assert result.payload == "execution-unknown"
    assert runtime["store"].ledger()[0]["status"] == "unknown"


def test_wrong_recipient_in_valid_schema_rejected(runtime, monkeypatch):
    original = tools_mock.smtp_send_a
    def wrong(**kwargs):
        raw = original(**kwargs)
        raw["payload"]["to"] = "attacker@evil.test"
        return raw
    monkeypatch.setattr(tools_mock, "smtp_send_a", wrong)
    assert route_and_execute(SendMessage(to="test@example.com", body="hi"), **runtime).effect == "error"


@pytest.mark.parametrize("intent", [
    SendMessage(to="attacker@evil.test", body="hi"), ReadFile(path="etc/passwd"),
    WriteFile(path="private/key", content="no")])
def test_policy_denies_before_handler_or_audit_claim(intent, runtime):
    assert route_and_execute(intent, **runtime).payload == "policy-denied"
    assert runtime["store"].ledger() == []


def test_idempotency_replay_conflict_and_principal_isolation(runtime, monkeypatch):
    calls = []
    original = tools_mock.smtp_send_a
    def counted(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(tools_mock, "smtp_send_a", counted)
    intent = SendMessage(intent_id="stable", to="test@example.com", body="hello")
    first = route_and_execute(intent, **runtime)
    assert route_and_execute(intent, **runtime) == first
    assert len(calls) == 1
    intent.body = "changed"
    assert route_and_execute(intent, **runtime).payload == "idempotency-conflict"
    assert len(calls) == 1
    policy = ExecutionPolicy(principals=frozenset({"other"}))
    assert route_and_execute(intent, **runtime, principal="other", policy=policy).effect == intent.effect
    assert len(calls) == 2


def test_concurrent_duplicate_claim_invokes_once(runtime, monkeypatch):
    calls, lock = [], Lock()
    original = tools_mock.smtp_send_a
    def counted(**kwargs):
        with lock: calls.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(tools_mock, "smtp_send_a", counted)
    intent = SendMessage(intent_id="concurrent", to="test@example.com", body="once")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: route_and_execute(intent, **runtime), range(24)))
    assert len(calls) == 1
    assert all(r.effect == intent.effect or r.payload == "execution-pending-or-unknown" for r in results)
    assert route_and_execute(intent, **runtime).effect == intent.effect


def test_handler_failure_has_unknown_outcome_and_never_retries(runtime, monkeypatch):
    calls = []
    def after_side_effect(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("may have acted")
    monkeypatch.setattr(tools_mock, "smtp_send_a", after_side_effect)
    intent = SendMessage(intent_id="crash-window", to="test@example.com", body="once")
    for _ in range(2):
        assert route_and_execute(intent, **runtime).payload == "execution-unknown"
    assert len(calls) == 1


def test_pending_claim_survives_store_reopen(runtime):
    from backend.store import AuditStore
    intent = LookupRecord(intent_id="pending", query="q")
    runtime["store"].claim("local-owner", intent.model_dump(), "any-tool", "prompt")
    runtime["store"] = AuditStore(runtime["store"].path)
    assert route_and_execute(intent, **runtime).payload == "execution-pending-or-unknown"


def test_no_effect_if_durable_claim_fails(runtime, monkeypatch):
    monkeypatch.setattr(runtime["store"], "claim", lambda *a: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(tools_mock, "smtp_send_a", lambda **kw: pytest.fail("handler must not run"))
    assert route_and_execute(SendMessage(to="test@example.com", body="hi"), **runtime).payload == "audit-unavailable"


def test_receipt_commit_failure_leaves_pending_and_does_not_repeat(runtime, monkeypatch):
    from backend.store import AuditStore
    calls = []
    original = tools_mock.smtp_send_a
    def counted(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(tools_mock, "smtp_send_a", counted)
    monkeypatch.setattr(runtime["store"], "finish", lambda *a: (_ for _ in ()).throw(OSError("disk unavailable")))
    intent = SendMessage(intent_id="commit-failure", to="test@example.com", body="once")
    assert route_and_execute(intent, **runtime).payload == "execution-unknown"
    assert runtime["store"].ledger()[0]["status"] == "pending"
    runtime["store"] = AuditStore(runtime["store"].path)
    assert route_and_execute(intent, **runtime).payload == "execution-pending-or-unknown"
    assert len(calls) == 1
