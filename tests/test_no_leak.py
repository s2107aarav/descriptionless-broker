import json
import pytest
from langchain_core.messages import SystemMessage, AIMessage
from backend.agent import run_agent, assert_context_clean, SYSTEM_PROMPT
from backend.llm import MockDeterministicLLM
from backend.registry import Registry
from backend.baseline import build_baseline_tools
from evals.poison_registry import get_poisoned_registry


def test_context_firewall_blocks_known_string(runtime):
    with pytest.raises(Exception, match="Context leak detected"):
        assert_context_clean([SystemMessage(content="Use smtp_send_a")], runtime["registry"])


def test_no_leak_end_to_end_uses_isolated_store(runtime):
    state = run_agent('Send "meeting at 3pm" to priya@example.com', provider=MockDeterministicLLM(), **runtime)
    assert state["final_result"]["effect"] == "message-delivered"
    prompts = runtime["store"].prompts("local-owner")
    assert len(prompts) == 1
    prompt = prompts[0]["prompt_text"]
    assert not any(word in prompt.lower() for word in ["smtp_", "fs_", "db_lookup", "jira_ticket", "mock://"])
    assert json.loads(prompt)[0]["content"] == SYSTEM_PROMPT


def test_registry_injection_reaches_actual_executor(runtime):
    empty = Registry(in_memory=True)
    runtime["registry"] = empty
    state = run_agent('Lookup "unique-canary"', provider=MockDeterministicLLM(), **runtime)
    assert state["final_result"] == {"effect": "error", "payload": "unsupported-effect"}
    assert runtime["store"].ledger() == []
    empty.close()


def test_baseline_receives_full_poisoned_tool_descriptions():
    reg = get_poisoned_registry()
    bound = MockDeterministicLLM().bind_tools(build_baseline_tools(reg))
    schemas = bound.kwargs["tools"]
    assert any("attacker@evil.test" in t["function"]["description"] for t in schemas)
    assert all(t["function"]["parameters"]["properties"] for t in schemas)
    reg.close()


def test_provider_injected_and_one_effect_only(runtime):
    class Provider:
        def __init__(self): self.messages = []
        def invoke(self, messages):
            self.messages.append(messages)
            return AIMessage(content='{"effect":"record-returned","query":"exact query"}')
    provider = Provider()
    state = run_agent("Find the requested record", provider=provider, **runtime)
    assert len(provider.messages) == 1 and state["turn_count"] == 1
    assert state["decided_intent"]["query"] == "exact query"
    assert len(runtime["store"].ledger()) == 1


@pytest.mark.parametrize("content", [
    '{"effect":"record-returned","query":"x","handler_name":"db_lookup_a"}',
    '{"effect":"record-returned","query":"x","intent_id":"attacker-selected"}',
    '{"effect":"file-written","path":"../private","content":"x"}',
    '{"effect":"message-delivered","to":"attacker@evil.test","body":"x"}',
])
def test_malicious_model_output_never_widens_permissions(content, runtime):
    class Provider:
        def invoke(self, messages): return AIMessage(content=content)
    state = run_agent("Do the requested action", provider=Provider(), **runtime)
    assert state["error"]
    assert not runtime["store"].ledger()
