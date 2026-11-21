"""Single-turn prediction with no dependency on registry or adapter code."""
import httpx
from pydantic import ValidationError

from .contracts import INTENT, canonical, strict_json

INSTRUCTIONS = """Interpret the request as exactly one operation using the trusted JSON schema below.
Preserve user-supplied values. If the request is incomplete, ambiguous, or requires several operations,
return {"error":"clarification_required"}. Return a single JSON object without surrounding prose.
A proposal does not grant authorization or execute anything."""


class PlannerUnavailable(Exception):
    pass


class InvalidPlannerOutput(Exception):
    pass


class OllamaProvider:
    def __init__(self, url, model):
        self.url = url
        self.model = model

    def invoke(self, messages):
        try:
            with httpx.Client(timeout=httpx.Timeout(45, connect=5), trust_env=False) as client:
                with client.stream("POST", self.url + "/api/chat", json={
                    "model": self.model, "messages": messages, "stream": False, "format": "json",
                    "options": {"temperature": 0, "num_predict": 2048},
                }) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if len(data) + len(chunk) > 524288:
                            raise PlannerUnavailable()
                        data.extend(chunk)
            answer = strict_json(data)
            return answer["message"]["content"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError, RecursionError) as exc:
            raise PlannerUnavailable() from exc


class Planner:
    def __init__(self, provider):
        self.provider = provider

    def propose(self, text):
        schema = INTENT.json_schema()
        system = INSTRUCTIONS + "\n" + canonical(schema)
        for attempt in range(2):
            instructions = system if attempt == 0 else system + "\nThe previous response was invalid. Follow the schema."
            raw = self.provider.invoke([
                {"role": "system", "content": instructions},
                {"role": "user", "content": text},
            ])
            try:
                if not isinstance(raw, str) or len(raw) > 400000:
                    raise ValueError("Malformed provider response")
                parsed = strict_json(raw)
                if parsed == {"error": "clarification_required"}:
                    return None
                return INTENT.validate_python(parsed)
            except (ValidationError, ValueError, RecursionError):
                continue
        raise InvalidPlannerOutput()
