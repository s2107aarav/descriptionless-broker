"""Single-effect LangGraph planner with explicit dependencies."""
import json
import uuid
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from .broker import replay_request, route_and_execute
from .intents import INTENT_ADAPTER
from .llm import get_llm
from .policy import ExecutionPolicy
from .registry import default_registry
from .store import AuditStore, canonical_json_hash


class ContextLeakError(Exception):
    pass


def assert_context_clean(messages, registry=None):
    active = registry if registry is not None else default_registry
    forbidden = {"smtp_", "fs_", "db_lookup", "jira_ticket", "mock://"}
    for row in active.all_tools():
        forbidden.update(str(row[k]).lower() for k in ("tool_id", "handler_name", "description") if len(str(row[k])) > 3)
    for message in messages:
        if message.type == "human":
            continue
        content = str(message.content).lower()
        if any(word in content for word in forbidden):
            raise ContextLeakError("Context leak detected; registry metadata string found")


SYSTEM_PROMPT = """You are a single-effect planner. Return one JSON object, no prose.
Choose exactly one effect and its required fields:
{"effect":"message-delivered","to":"recipient@example.com","body":"exact message"}
{"effect":"file-content-returned","path":"workspace/relative.txt"}
{"effect":"file-written","path":"workspace/relative.txt","content":"exact content"}
{"effect":"record-returned","query":"exact query"}
{"effect":"ticket-created","title":"exact title","detail":"exact detail"}
Use exact user-requested arguments; do not invent missing values. No other fields.
If the request is ambiguous or cannot be represented, return {"error":"clarification-required"}.
You cannot authorize an effect, choose an implementation, or widen permissions."""

RETRY_PROMPT = "Previous output was invalid. Return one valid effect JSON or request clarification."


class AgentState(TypedDict):
    user_text: str
    history: list
    turn_count: int
    retry_count: int
    current_intent: object
    final_result: object
    error: str
    prompt_hash: str
    prompt_turn_id: str
    decided_intent: object


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate planner field")
        result[key] = value
    return result


def _execution_fields(result):
    public = result.model_dump()
    return {"history": [public], "final_result": public, "current_intent": None,
            "turn_count": 1, "error": result.payload if result.effect == "error" else ""}


def _failure(code):
    return {"final_result": None, "error": code, "turn_count": 0, "history": []}


def run_agent(user_text, *, registry=None, store=None, policy=None, provider=None,
              principal="local-owner", idempotency_key=None):
    if not isinstance(user_text, str) or not user_text.strip() or len(user_text) > 12000:
        return _failure("invalid-request")
    try:
        request_hash = canonical_json_hash(user_text)
    except UnicodeEncodeError:
        return _failure("invalid-request")

    active_registry = registry if registry is not None else default_registry
    active_policy = policy if policy is not None else ExecutionPolicy()
    key = idempotency_key if idempotency_key is not None else uuid.uuid4().hex
    try:
        active_store = store if store is not None else AuditStore()
    except Exception:
        return _failure("audit-unavailable")
    if idempotency_key is not None:
        prior = replay_request(active_store, principal, key, request_hash, active_policy)
        if prior is not None:
            return _execution_fields(prior)
    try:
        active_provider = provider if provider is not None else get_llm()
    except Exception:
        return _failure("provider-or-audit-unavailable")

    def decide(state):
        instructions = SYSTEM_PROMPT + "\n" + RETRY_PROMPT if state["retry_count"] else SYSTEM_PROMPT
        messages = [SystemMessage(content=instructions), HumanMessage(content=state["user_text"])]
        try:
            assert_context_clean(messages, active_registry)
            model = getattr(active_provider, "model", None) or getattr(active_provider, "_llm_type", type(active_provider).__name__)
            prompt_turn_id = uuid.uuid4().hex
            prompt_hash = active_store.record_prompt(principal, prompt_turn_id, messages, str(model))
            response = active_provider.invoke(messages)
        except ContextLeakError:
            return {"error": "context-metadata-blocked", "retry_count": 2, "current_intent": None}
        except Exception:
            return {"error": "provider-or-audit-unavailable", "retry_count": 2, "current_intent": None}

        try:
            # A 16,000-character write can be serialized as escaped Unicode pairs.
            if not isinstance(response.content, str) or len(response.content) > 200000:
                raise ValueError("Invalid model output")
            parsed = json.loads(response.content, object_pairs_hook=_unique_fields)
            if parsed == {"error": "clarification-required"}:
                return {"error": "clarification-required", "retry_count": 2, "current_intent": None}
            if not isinstance(parsed, dict) or set(parsed) & {"intent_id", "schema_version"}:
                raise ValueError("Planner may not select request identity/version")
            intent = INTENT_ADAPTER.validate_python({**parsed, "intent_id": key})
        except (AttributeError, ValueError, TypeError, RecursionError):
            return {"error": "invalid-intent", "retry_count": state["retry_count"] + 1, "current_intent": None}
        return {"current_intent": intent, "decided_intent": intent.model_dump(), "prompt_hash": prompt_hash,
                "prompt_turn_id": prompt_turn_id, "error": ""}

    def execute(state):
        result = route_and_execute(state["current_intent"], prompt_hash=state["prompt_hash"],
                                   registry=active_registry, store=active_store, policy=active_policy, principal=principal,
                                   request_hash=request_hash, prompt_turn_id=state["prompt_turn_id"])
        return _execution_fields(result)

    def next_node(state):
        if state.get("current_intent") is not None:
            return "broker_exec"
        return "decide_intent" if state["retry_count"] < 2 else END

    workflow = StateGraph(AgentState)
    workflow.add_node("decide_intent", decide)
    workflow.add_node("broker_exec", execute)
    workflow.set_entry_point("decide_intent")
    workflow.add_conditional_edges("decide_intent", next_node, {"broker_exec": "broker_exec", "decide_intent": "decide_intent", END: END})
    workflow.add_edge("broker_exec", END)
    return workflow.compile().invoke({
        "user_text": user_text, "history": [], "turn_count": 0, "retry_count": 0,
        "current_intent": None, "final_result": None, "error": "", "prompt_hash": "", "decided_intent": None})
