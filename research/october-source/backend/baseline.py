import sqlite3
from typing import TypedDict, List, Dict, Any, Literal
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from pydantic import create_model

from .llm import get_llm
from .registry import Registry

def build_baseline_tools(registry: Registry) -> List[StructuredTool]:
    tools = []
    
    with sqlite3.connect(registry.db_path, uri=registry.db_path.startswith("file:")) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM tools WHERE enabled = 1 ORDER BY priority ASC, tool_id ASC").fetchall()
    
    for r in rows:
        handler_name = r["handler_name"]
        description = r["description"]
        effect = r["effect"]
        
        handler = registry.get_handler(handler_name)
        
        if effect == "message-delivered":
            Schema = create_model(handler_name, to=(str, ...), body=(str, ...))
        elif effect == "file-content-returned":
            Schema = create_model(handler_name, path=(str, ...))
        elif effect == "file-written":
            Schema = create_model(handler_name, path=(str, ...), content=(str, ...))
        elif effect == "record-returned":
            Schema = create_model(handler_name, query=(str, ...))
        elif effect == "ticket-created":
            Schema = create_model(handler_name, title=(str, ...), detail=(str, ...))
        else:
            Schema = create_model(handler_name)
            
        def make_func(h):
            def func(**kwargs):
                return h(**kwargs)
            return func
            
        tools.append(StructuredTool.from_function(
            func=make_func(handler),
            name=handler_name,
            description=description,
            args_schema=Schema
        ))
    return tools

def run_baseline_agent(user_text: str, registry: Registry, provider=None):
    llm = provider if provider is not None else get_llm()
    tools = build_baseline_tools(registry)
    
    if not tools:
        return {"final_result": None, "tool_name": None}
        
    llm_with_tools = llm.bind_tools(tools)
    messages = [
        SystemMessage(content="You are a helpful assistant with access to tools. Solve the user's request by calling the appropriate tool."),
        HumanMessage(content=user_text)
    ]
    
    res = llm_with_tools.invoke(messages)
    
    if hasattr(res, "tool_calls") and res.tool_calls:
        tc = res.tool_calls[0]
        t = next((x for x in tools if x.name == tc["name"]), None)
        if t:
            tool_res = t.invoke(tc["args"])
            # Return result and the tool_name picked by LLM (baseline)
            return {"final_result": tool_res, "tool_name": tc["name"], "arguments": tc["args"]}
            
    return {"final_result": None, "tool_name": None}
