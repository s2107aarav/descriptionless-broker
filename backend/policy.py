"""Trusted capability policy, independent of user text and model-generated JSON."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExecutionPolicy:
    principals: frozenset = field(default_factory=lambda: frozenset({"local-owner"}))
    recipients: frozenset = field(default_factory=lambda: frozenset({"priya@example.com", "team@example.com", "test@example.com"}))
    path_prefixes: tuple = ("workspace/", "reports/")
    allowed_effects: frozenset = field(default_factory=lambda: frozenset({
        "message-delivered", "file-content-returned", "file-written", "record-returned", "ticket-created"}))

    def authorize(self, principal, intent):
        if principal not in self.principals or intent.effect not in self.allowed_effects:
            return False
        if intent.effect == "message-delivered":
            return intent.to in self.recipients
        if intent.effect in {"file-written", "file-content-returned"}:
            return any(intent.path.startswith(prefix) for prefix in self.path_prefixes)
        return True
