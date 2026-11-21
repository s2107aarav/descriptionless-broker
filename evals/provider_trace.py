"""Evaluation-only capture of public model inputs/outputs, including tool schemas.

No credentials or provider internals are captured. Evaluation tasks are synthetic;
do not use these artifacts for private prompts without an explicit retention policy.
"""
from langchain_core.utils.function_calling import convert_to_openai_tool


class TracedProvider:
    def __init__(self, provider, traces=None, tool_schemas=None):
        self.provider = provider
        self.traces = traces if traces is not None else []
        self.tool_schemas = tool_schemas or []
        self.model = str(getattr(provider, "model", getattr(provider, "_llm_type", type(provider).__name__)))

    def bind_tools(self, tools, **kwargs):
        return TracedProvider(self.provider.bind_tools(tools, **kwargs), self.traces,
                              [convert_to_openai_tool(tool) for tool in tools])

    def invoke(self, messages):
        trace = {"messages": [m.model_dump(mode="json") for m in messages],
                 "tool_schemas": self.tool_schemas}
        self.traces.append(trace)
        try:
            response = self.provider.invoke(messages)
            trace["response"] = response.model_dump(mode="json")
            return response
        except Exception as exc:
            trace["error_type"] = type(exc).__name__
            raise
