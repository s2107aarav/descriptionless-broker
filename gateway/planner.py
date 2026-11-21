"""Single-turn prediction with no dependency on registry or adapter code."""
import time

import httpx

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
    def __init__(self, url, model, *, timeout_seconds=45):
        self.url = url
        self.model = model
        self.timeout_seconds = timeout_seconds

    def invoke(self, messages):
        try:
            deadline = time.monotonic() + self.timeout_seconds
            with httpx.Client(timeout=httpx.Timeout(self.timeout_seconds, connect=min(5, self.timeout_seconds)),
                              trust_env=False, follow_redirects=False) as client:
                with client.stream("POST", self.url + "/api/chat", json={
                    "model": self.model, "messages": messages, "stream": False, "format": "json",
                    "options": {"temperature": 0, "num_predict": 2048},
                }) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() > deadline or len(data) + len(chunk) > 524288:
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
        return propose_json(self.provider, text, INSTRUCTIONS + "\n" + canonical(INTENT.json_schema()),
                            INTENT.validate_python)


def propose_json(provider, text, system, validate):
    for attempt in range(2):
        instructions = system if attempt == 0 else system + "\nThe previous response was invalid. Follow the schema."
        raw = provider.invoke([
            {"role": "system", "content": instructions},
            {"role": "user", "content": text},
        ])
        try:
            if not isinstance(raw, str) or len(raw) > 400000:
                raise ValueError("Malformed provider response")
            parsed = strict_json(raw)
            if parsed == {"error": "clarification_required"}:
                return None
            return validate(parsed)
        except (ValueError, TypeError, RecursionError):
            continue
    raise InvalidPlannerOutput()
