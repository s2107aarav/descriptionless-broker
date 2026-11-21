"""Explicit provider selection. Offline parsing is plumbing, not model evidence."""
import json
import os
import re
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.utils.function_calling import convert_to_openai_tool


def offline_parse(text):
    """Small documented demo grammar; never fabricates placeholders."""
    if text.startswith("INTENT: "):
        # The planner validates the raw JSON, including duplicate field names.
        return json.loads(text[len("INTENT: "):])
    match = re.fullmatch(r'Send "(.*)" to ([^ ]+)', text, re.S)
    if match:
        return {"effect": "message-delivered", "to": match[2], "body": match[1]}
    match = re.fullmatch(r'Read ([^ ]+)', text)
    if match:
        return {"effect": "file-content-returned", "path": match[1]}
    match = re.fullmatch(r'Write "(.*)" to ([^ ]+)', text, re.S)
    if match:
        return {"effect": "file-written", "path": match[2], "content": match[1]}
    match = re.fullmatch(r'Lookup "(.*)"', text, re.S)
    if match:
        return {"effect": "record-returned", "query": match[1]}
    match = re.fullmatch(r'Create ticket "(.*)" with detail "(.*)"', text, re.S)
    if match:
        return {"effect": "ticket-created", "title": match[1], "detail": match[2]}
    return {"error": "clarification-required"}


class MockDeterministicLLM(BaseChatModel):
    @property
    def _llm_type(self):
        return "offline_grammar_stub"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        text = next((m.content for m in reversed(messages) if m.type == "human"), "")
        tools = kwargs.get("tools")
        if not tools and text.startswith("INTENT: "):
            msg = AIMessage(content=text[len("INTENT: "):])
            return ChatResult(generations=[ChatGeneration(message=msg)])
        try:
            parsed = offline_parse(text)
        except (ValueError, RecursionError):
            parsed = {"error": "clarification-required"}
        if tools:
            prefixes = {"message-delivered": "smtp", "file-content-returned": "fs_read",
                        "file-written": "fs_write", "record-returned": "db_lookup", "ticket-created": "jira_ticket"}
            effect = parsed.get("effect") if isinstance(parsed, dict) else None
            prefix = prefixes.get(effect) if isinstance(effect, str) else None
            selected = next((t for t in tools if prefix and t["function"]["name"].startswith(prefix)), None)
            msg = AIMessage(content="", tool_calls=[{"name": selected["function"]["name"],
                "args": {k: v for k, v in parsed.items() if k != "effect"}, "id": "offline-call"}]) if selected else AIMessage(content="No compatible tool")
        else:
            msg = AIMessage(content=json.dumps(parsed, ensure_ascii=False))
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools, **kwargs):
        # Preserve names, descriptions AND complete parameter schemas.
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)


def get_llm(provider=None, model=None):
    provider = provider or os.getenv("PROVIDER", "mock")
    if provider == "mock":
        return MockDeterministicLLM()
    if provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=model or os.getenv("MODEL", "qwen2.5:3b"), temperature=0,
                         base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
                         num_predict=512, client_kwargs={"timeout": 60})
    if provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=model or os.environ["MODEL"], temperature=0)
    raise ValueError("Unknown provider")
