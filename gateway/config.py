"""Private local runtime configuration, capabilities and trusted routing."""
import hmac
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, StringConstraints, TypeAdapter

from .contracts import Closed, OPERATIONS, Slug, fingerprint, strict_json

Role = Literal["requester", "agent", "reviewer"]
SCOPE = "demo"
APPROVAL_SECONDS = 15 * 60


def default_data_directory():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Descriptionless Broker" / "gateway"
    state = Path(os.getenv("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    if not state.is_absolute():
        raise ValueError("XDG_STATE_HOME must be an absolute path")
    return state / "descriptionless-broker" / "gateway"


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role
    scope: str = SCOPE


class Policy(Closed):
    requester_operations: list[str] = Field(default_factory=lambda: sorted(OPERATIONS), max_length=6)
    agent_operations: list[str] = Field(default_factory=lambda: sorted(OPERATIONS - {"read-file"}), max_length=6)
    write_namespaces: list[Literal["workspace", "reports"]] = Field(default_factory=lambda: ["reports"], max_length=2)
    read_namespaces: list[Literal["workspace", "reports"]] = Field(default_factory=lambda: ["workspace", "reports"], max_length=2)
    recipients: list[Slug] = Field(default_factory=lambda: ["requester", "reviewer", "agent"], max_length=50)

    @property
    def revision(self) -> str:
        return fingerprint(self.model_dump())

    def authorize(self, principal, intent) -> bool:
        if principal.scope != SCOPE or principal.role == "reviewer":
            return False
        operations = self.agent_operations if principal.role == "agent" else self.requester_operations
        if intent.operation not in operations:
            return False
        if intent.operation == "send-message":
            return intent.recipient in self.recipients
        if intent.operation == "write-file":
            return intent.path.split("/", 1)[0] in self.write_namespaces
        if intent.operation == "read-file":
            return intent.path.split("/", 1)[0] in self.read_namespaces
        return True


class AdapterSpec(Closed):
    adapter_id: Slug
    operation: str
    implementation: Slug
    version: Literal["1"] = "1"
    priority: Annotated[int, Field(ge=0, le=1000)] = 10
    enabled: bool = True
    description: Annotated[str, StringConstraints(max_length=512)] = ""

    def routing_identity(self):
        return self.model_dump(exclude={"description"})


def default_adapters():
    return [AdapterSpec(adapter_id=f"local-{name}", implementation=name, operation=name) for name in sorted(OPERATIONS)]


class Registry:
    def __init__(self, entries):
        self.entries = tuple(sorted(entries, key=lambda entry: (entry.priority, entry.adapter_id)))
        if len({entry.adapter_id for entry in self.entries}) != len(self.entries):
            raise ValueError("Duplicate adapter ID")
        for entry in self.entries:
            if entry.operation not in OPERATIONS or entry.implementation != entry.operation:
                raise ValueError("Adapter implementation is incompatible with its operation")
        self.revision = fingerprint([entry.routing_identity() for entry in self.entries])

    def resolve(self, operation):
        return next((entry for entry in self.entries if entry.enabled and entry.operation == operation), None)


def parse_config(path, parse, default):
    try:
        with path.open("rb") as source:
            raw = source.read(65537)
    except FileNotFoundError:
        return default()
    if len(raw) > 65536:
        raise ValueError(f"{path.name} exceeds its size limit")
    return parse(strict_json(raw))


def load_policy(root):
    policy = parse_config(root / "policy.json", Policy.model_validate, Policy)
    if (set(policy.requester_operations) | set(policy.agent_operations)) - OPERATIONS:
        raise ValueError("Unknown operation in policy")
    return policy


def load_registry(root):
    adapter_list = TypeAdapter(Annotated[list[AdapterSpec], Field(max_length=50)])
    return Registry(parse_config(root / "registry.json", adapter_list.validate_python, default_adapters))


def ensure_private_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError(f"{path.name} must be an owned, private directory")


def ensure_private_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1:
        raise RuntimeError(f"{path.name} must be an owned, private regular file")


@dataclass(frozen=True)
class Settings:
    root: Path
    frontend_origins: tuple[str, ...] = ("http://localhost:3000", "http://127.0.0.1:3000")
    model: str | None = None
    ollama_url: str = "http://127.0.0.1:11434"

    @classmethod
    def from_env(cls):
        model = os.getenv("GATEWAY_OLLAMA_MODEL") or None
        url = os.getenv("GATEWAY_OLLAMA_URL", "http://127.0.0.1:11434")
        origins = tuple(origin.strip() for origin in os.getenv(
            "GATEWAY_FRONTEND_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if origin.strip())
        validate_local_url(url)
        for origin in origins:
            validate_local_url(origin)
        if model and (len(model) > 128 or not model.isascii() or any(char.isspace() for char in model)):
            raise ValueError("Invalid GATEWAY_OLLAMA_MODEL")
        configured = os.getenv("GATEWAY_DATA_DIR")
        root = Path(configured).absolute() if configured else default_data_directory()
        return cls(root, origins, model, url)


def validate_local_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or parsed.port is None):
        raise ValueError("Configure a loopback HTTP URL with a port")


def same_digest(first, second):
    return hmac.compare_digest(first, second)
