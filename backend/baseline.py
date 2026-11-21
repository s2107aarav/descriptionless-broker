"""Single-call baseline with ordinary model-visible tool metadata."""
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from pydantic import ConfigDict, create_model

from .intents import INTENT_TYPES
from .llm import get_llm
from .registry import Registry

EFFECT_ARGUMENTS = {
    intent.model_fields["effect"].default: tuple(intent.model_fields.keys() - {"effect", "intent_id", "schema_version"})
    for intent in INTENT_TYPES
}


def build_baseline_tools(registry: Registry) -> list[StructuredTool]:
    tools = []
    names = set()
    for row in registry.all_tools(enabled_only=True):
        name = row["handler_name"]
        if name in names:
            raise ValueError("Duplicate baseline tool name")
        names.add(name)
        fields = {key: (str, ...) for key in sorted(EFFECT_ARGUMENTS[row["effect"]])}
        schema = create_model(name, __config__=ConfigDict(extra="forbid", strict=True), **fields)
        tools.append(StructuredTool.from_function(
            func=registry.get_handler(name), name=name, description=row["description"], args_schema=schema,
        ))
    return tools


def run_baseline_agent(user_text: str, registry: Registry, provider=None):
    tools = build_baseline_tools(registry)
    if not tools:
        return {"final_result": None, "tool_name": None}

    llm = provider if provider is not None else get_llm()
    messages = [
        SystemMessage(content="You are a helpful assistant with access to tools. Solve the user's request by calling the appropriate tool."),
        HumanMessage(content=user_text),
    ]
    response = llm.bind_tools(tools).invoke(messages)
    calls = getattr(response, "tool_calls", [])
    if len(calls) != 1 or getattr(response, "invalid_tool_calls", []):
        return {"final_result": None, "tool_name": None}

    call = calls[0]
    selected = next((tool for tool in tools if tool.name == call["name"]), None)
    if selected is None:
        return {"final_result": None, "tool_name": None}
    return {"final_result": selected.invoke(call["args"]), "tool_name": selected.name, "arguments": call["args"]}
