"""Policy-enforced deterministic routing with pre-execution idempotency."""
import time
from pydantic import BaseModel, ValidationError
from .intents import BaseIntent, INTENT_SCHEMA_VERSION, INTENT_TYPES, INTENT_ADAPTER
from .registry import Registry, default_registry
from .store import AuditStore
from .policy import ExecutionPolicy
from .contracts import validate_output


class BrokerResult(BaseModel):
    effect: str
    payload: dict | str


def route_and_execute(intent, prompt_hash="", registry=None, store=None, policy=None, principal="local-owner"):
    if not isinstance(intent, BaseIntent):
        return BrokerResult(effect="error", payload="invalid-intent")
    if intent.schema_version != INTENT_SCHEMA_VERSION:
        return BrokerResult(effect="error", payload="invalid-schema-version")
    if type(intent) not in INTENT_TYPES:
        return BrokerResult(effect="error", payload="unsupported-effect")
    try:
        intent = INTENT_ADAPTER.validate_python(intent.model_dump())
    except ValidationError:
        return BrokerResult(effect="error", payload="invalid-intent")
    active_policy = policy if policy is not None else ExecutionPolicy()
    if not active_policy.authorize(principal, intent):
        return BrokerResult(effect="error", payload="policy-denied")
    active_registry = registry if registry is not None else default_registry
    tools = active_registry.get_enabled_tools_for_effect(intent.effect)
    if not tools:
        return BrokerResult(effect="error", payload="unsupported-effect")
    chosen = tools[0]
    try:
        handler = active_registry.get_handler(chosen.handler_name)
    except (KeyError, AttributeError):
        return BrokerResult(effect="error", payload="unavailable-handler")
    active_store = store if store is not None else AuditStore()
    try:
        status, prior = active_store.claim(principal, intent.model_dump(), chosen.tool_id, prompt_hash)
    except Exception:
        # No effect may run without a durable execution claim.
        return BrokerResult(effect="error", payload="audit-unavailable")
    if status == "completed":
        return BrokerResult.model_validate(prior)
    if status != "new":
        return BrokerResult(effect="error", payload={"conflict": "idempotency-conflict",
                           "pending": "execution-pending-or-unknown", "unknown": "execution-unknown"}.get(status, "execution-unknown"))
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
        result = BrokerResult(effect="error", payload="execution-unknown")
        try:
            active_store.finish(principal, intent.intent_id, result.model_dump(), (time.perf_counter()-start)*1000, "unknown")
        except Exception:
            pass  # durable pending claim still prevents a repeat
        return result
