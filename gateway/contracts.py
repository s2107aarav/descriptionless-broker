"""Human-governed V2 intents and adapter result contracts."""
import hashlib
import json
from typing import Annotated, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

Slug = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")]
Title = Annotated[str, StringConstraints(min_length=1, max_length=120, pattern=r"\S")]
Detail = Annotated[str, StringConstraints(min_length=1, max_length=4000, pattern=r"\S")]
FilePath = Annotated[str, StringConstraints(min_length=1, max_length=240, pattern=r"^(workspace|reports)(/[a-zA-Z0-9_-][a-zA-Z0-9_.-]{0,95}){1,8}$")]
Status = Literal["open", "investigating", "resolved"]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

MAX_FILE_BYTES = 65536


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SendMessage(Closed):
    schema_version: Literal["v2"]
    operation: Literal["send-message"]
    recipient: Slug
    body: Detail


class ReadFile(Closed):
    schema_version: Literal["v2"]
    operation: Literal["read-file"]
    path: FilePath


class WriteFile(Closed):
    schema_version: Literal["v2"]
    operation: Literal["write-file"]
    path: FilePath
    content: Annotated[str, StringConstraints(max_length=MAX_FILE_BYTES)]

    @model_validator(mode="after")
    def limit_bytes(self) -> Self:
        if len(self.content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("File exceeds the byte limit")
        return self


class LookupRecord(Closed):
    schema_version: Literal["v2"]
    operation: Literal["lookup-record"]
    collection: Literal["services", "tickets"]
    record_id: Slug


class CreateTicket(Closed):
    schema_version: Literal["v2"]
    operation: Literal["create-ticket"]
    title: Title
    detail: Detail


class UpdateTicket(Closed):
    schema_version: Literal["v2"]
    operation: Literal["update-ticket"]
    ticket_id: Slug
    expected_revision: Annotated[int, Field(ge=1, le=2147483647)]
    status: Status | None = None
    detail: Detail | None = None

    @model_validator(mode="after")
    def require_changes(self) -> Self:
        if self.status is None and self.detail is None:
            raise ValueError("Provide a status or detail change")
        return self


Intent = Annotated[
    SendMessage | ReadFile | WriteFile | LookupRecord | CreateTicket | UpdateTicket,
    Field(discriminator="operation"),
]
INTENT = TypeAdapter(Intent)
INTENT_TYPES = (SendMessage, ReadFile, WriteFile, LookupRecord, CreateTicket, UpdateTicket)
OPERATION_BY_TYPE = {model: get_args(model.model_fields["operation"].annotation)[0] for model in INTENT_TYPES}
OPERATIONS = frozenset(OPERATION_BY_TYPE.values())
READ_OPERATIONS = frozenset({"lookup-record", "read-file"})


class MessageReceipt(Closed):
    delivery: Literal["delivered_to_local_inbox"]
    message_id: Slug
    recipient: Slug


class FileReceipt(Closed):
    path: FilePath
    bytes: Annotated[int, Field(ge=0, le=MAX_FILE_BYTES)]
    sha256: Digest


class FileReadReceipt(FileReceipt):
    content: Annotated[str, StringConstraints(max_length=MAX_FILE_BYTES)]


class TicketReceipt(Closed):
    ticket_id: Slug
    revision: Annotated[int, Field(ge=1, le=2147483647)]
    status: Status


class ServiceRecord(Closed):
    record_id: Slug
    name: Title
    description: Detail


class TicketRecord(TicketReceipt):
    title: Title
    detail: Detail


class RecordReceipt(Closed):
    collection: Literal["services", "tickets"]
    record: ServiceRecord | TicketRecord


RECEIPTS = {
    "send-message": MessageReceipt,
    "read-file": FileReadReceipt,
    "write-file": FileReceipt,
    "lookup-record": RecordReceipt,
    "create-ticket": TicketReceipt,
    "update-ticket": TicketReceipt,
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fingerprint(value) -> str:
    return digest(canonical(value).encode("utf-8"))


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate JSON field")
        obj[key] = value
    return obj


def strict_json(raw):
    def reject_constant(_):
        raise ValueError("Invalid JSON numeric constant")
    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
