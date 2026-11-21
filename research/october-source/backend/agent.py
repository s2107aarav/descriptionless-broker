"""Single-effect LangGraph planner with explicit dependencies.

Registry metadata is not used to construct model context. The string guard is
a defense-in-depth diagnostic, not a proof of information-flow noninterference.
Tool outputs are returned to the caller and are NOT fed into another model turn.
"""
import json
import uuid
from typing import TypedDict
from pydantic import ValidationError
from langchain_core.messages import SystemMessage, HumanMessage
from .intents import INTENT_ADAPTER
from .llm import get_llm
from .registry import default_registry
from .store import AuditStore
from .policy import ExecutionPolicy
from .broker import route_and_execute


class ContextLeakError(Exception):
    pass


def assert_context_clean(messages, registry=None):
    active = registry if registry is not None else default_registry
    forbidden = {"smtp_", "fs_", "db_lookup", "jira_ticket", "mock://"}
    for row in active.all_tools():
        forbidden.update(str(row[k]).lower() for k in ("tool_id", "handler_name", "description") if len(str(row[k])) > 3)
    for message in messages:
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


class AgentState(TypedDict):
    user_text: str
    history: list
    turn_count: int
    retry_count: int
    current_intent: object
    final_result: object
    error: str
    prompt_hash: str
    decided_intent: object


def run_agent(user_text, *, registry=None, store=None, policy=None, provider=None,
              principal="local-owner", idempotency_key=None):
    from langgraph.graph import StateGraph, END
    if not isinstance(user_text, str) or not user_text.strip() or len(user_text) > 12000:
        return {"final_result": None, "error": "invalid-request", "turn_count": 0, "history": []}
    active_registry = registry if registry is not None else default_registry
    active_store = store if store is not None else AuditStore()
    active_policy = policy if policy is not None else ExecutionPolicy()
    active_provider = provider if provider is not None else get_llm()
    key = idempotency_key or uuid.uuid4().hex

    def decide(state):
        messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=state["user_text"])]
        if state.get("error"):
            messages.append(SystemMessage(content="Previous output was invalid. Return one valid effect JSON or request clarification."))
        try:
            assert_context_clean(messages, active_registry)
            model = str(getattr(active_provider, "model", getattr(active_provider, "_llm_type", type(active_provider).__name__)))
            prompt_hash = active_store.record_prompt(principal, uuid.uuid4().hex, messages, model)
            response = active_provider.invoke(messages)
            if not isinstance(response.content, str) or len(response.content) > 24000:
                raise ValueError("Invalid model output")
            parsed = json.loads(response.content)
            if parsed == {"error": "clarification-required"}:
                return {**state, "error": "clarification-required", "retry_count": 2, "current_intent": None}
            if not isinstance(parsed, dict) or set(parsed) & {"intent_id", "schema_version"}:
                raise ValueError("Planner may not select request identity/version")
            intent = INTENT_ADAPTER.validate_python({**parsed, "intent_id": key})
            return {**state, "current_intent": intent, "decided_intent": intent.model_dump(),
                    "prompt_hash": prompt_hash, "error": ""}
        except ContextLeakError:
            return {**state, "error": "context-metadata-blocked", "retry_count": 2, "current_intent": None}
        except (ValidationError, ValueError, TypeError):
            return {**state, "error": "invalid-intent", "retry_count": state["retry_count"] + 1, "current_intent": None}
        except Exception:
            return {**state, "error": "provider-or-audit-unavailable", "retry_count": 2, "current_intent": None}

    def execute(state):
        result = route_and_execute(state["current_intent"], prompt_hash=state["prompt_hash"],
                                   registry=active_registry, store=active_store, policy=active_policy, principal=principal)
        public = result.model_dump()
        return {**state, "history": [public], "final_result": public, "current_intent": None,
                "turn_count": 1, "error": result.payload if result.effect == "error" else ""}

    def next_node(state):
        if state.get("current_intent") is not None:
            return "broker_exec"
        return "decide_intent" if state["retry_count"] < 2 else "__end__"

    workflow = StateGraph(AgentState)
    workflow.add_node("decide_intent", decide)
    workflow.add_node("broker_exec", execute)
    workflow.set_entry_point("decide_intent")
    workflow.add_conditional_edges("decide_intent", next_node)
    workflow.add_edge("broker_exec", END)
    return workflow.compile().invoke({
        "user_text": user_text, "history": [], "turn_count": 0, "retry_count": 0,
        "current_intent": None, "final_result": None, "error": "", "prompt_hash": "", "decided_intent": None})
