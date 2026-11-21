"""Policy-enforced deterministic routing with pre-execution idempotency."""
import time
from pydantic import BaseModel, ValidationError
from .intents import BaseIntent, INTENT_SCHEMA_VERSION, INTENT_TYPES, INTENT_ADAPTER
from .registry import default_registry
from .store import AuditStore, canonical_json_hash
from .policy import ExecutionPolicy
from .contracts import validate_output


class BrokerResult(BaseModel):
    effect: str
    payload: dict | str


def _error(code):
    return BrokerResult(effect="error", payload=code)


def _prior_result(status, row, principal, policy):
    if status == "conflict":
        return _error("idempotency-conflict")
    try:
        intent = INTENT_ADAPTER.validate_json(row["request_json"])
        if not policy.authorize(principal, intent):
            return _error("policy-denied")
        if status == "completed":
            result = BrokerResult.model_validate_json(row["result_json"])
            return BrokerResult(effect=intent.effect, payload=validate_output(intent, result.model_dump()))
    except (TypeError, ValueError):
        return _error("audit-unavailable")
    return _error("execution-pending-or-unknown" if status == "pending" else "execution-unknown")


def replay_request(store, principal, key, request_hash, policy):
    try:
        prior = store.lookup_claim(principal, key, request_hash=request_hash)
    except Exception:
        return _error("audit-unavailable")
    return _prior_result(*prior, principal, policy) if prior is not None else None


def route_and_execute(intent, prompt_hash="", registry=None, store=None, policy=None, principal="local-owner",
                      *, request_hash=None, prompt_turn_id=None):
    if not isinstance(intent, BaseIntent):
        return _error("invalid-intent")
    if intent.schema_version != INTENT_SCHEMA_VERSION:
        return _error("invalid-schema-version")
    if type(intent) not in INTENT_TYPES:
        return _error("unsupported-effect")
    try:
        intent = INTENT_ADAPTER.validate_python(intent.model_dump())
    except ValidationError:
        return _error("invalid-intent")
    active_policy = policy if policy is not None else ExecutionPolicy()
    if not active_policy.authorize(principal, intent):
        return _error("policy-denied")
    request = intent.model_dump()
    try:
        active_store = store if store is not None else AuditStore()
        prior = active_store.lookup_claim(principal, intent.intent_id, request_hash=request_hash,
                                          fingerprint=canonical_json_hash(request))
    except Exception:
        return _error("audit-unavailable")
    if prior is not None:
        return _prior_result(*prior, principal, active_policy)
    active_registry = registry if registry is not None else default_registry
    try:
        tools = active_registry.get_enabled_tools_for_effect(intent.effect)
    except Exception:
        return _error("registry-unavailable")
    if not tools:
        return _error("unsupported-effect")
    chosen = tools[0]
    try:
        handler = active_registry.get_handler(chosen.handler_name)
    except (KeyError, AttributeError):
        return _error("unavailable-handler")
    try:
        status, prior = active_store.claim(principal, request, chosen.tool_id, prompt_hash,
                                           request_hash=request_hash, prompt_turn_id=prompt_turn_id)
    except Exception:
        # No effect may run without a durable execution claim.
        return _error("audit-unavailable")
    if status != "new":
        return _prior_result(status, prior, principal, active_policy)
    start = time.perf_counter()
    try:
        kwargs = intent.model_dump(exclude={"intent_id", "schema_version", "effect"})
        raw = handler(**kwargs)
        result = BrokerResult(effect=intent.effect, payload=validate_output(intent, raw))
        active_store.finish(principal, intent.intent_id, result.model_dump(), (time.perf_counter()-start)*1000)
        return result
    except Exception:
        # The handler may have acted before raising/returning invalid output.
        # Never automatically retry an ambiguous side effect.
        result = _error("execution-unknown")
        try:
            active_store.finish(principal, intent.intent_id, result.model_dump(), (time.perf_counter()-start)*1000, "unknown")
        except Exception:
            pass  # durable pending claim still prevents a repeat
        return result
