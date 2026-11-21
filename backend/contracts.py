"""Closed, bounded effect output schemas, with request/result binding checks."""
from typing import Literal, Annotated
from pydantic import BaseModel, ConfigDict, Field
import hashlib


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MessagePayload(Payload):
    status: Literal["sent"]
    to: str = Field(max_length=254)
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReadPayload(Payload):
    path: str = Field(max_length=128)
    content: str = Field(max_length=16000)


class WritePayload(Payload):
    path: str = Field(max_length=128)
    bytes_written: int = Field(ge=0, le=64000)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecordPayload(Payload):
    query: str = Field(max_length=500)
    results: list[Annotated[str, Field(max_length=2000)]] = Field(max_length=20)


class TicketPayload(Payload):
    ticket_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(max_length=120)
    detail_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


SCHEMAS = {"message-delivered": MessagePayload, "file-content-returned": ReadPayload,
           "file-written": WritePayload, "record-returned": RecordPayload, "ticket-created": TicketPayload}


def validate_output(intent, raw):
    # Envelope metadata is audit-only. Unknown envelope keys and every unknown
    # payload key fail closed, regardless of casing or nesting.
    if not isinstance(raw, dict) or set(raw) - {"effect", "payload", "handler_name", "endpoint", "provider"}:
        raise ValueError("Invalid envelope")
    if raw.get("effect") != intent.effect:
        raise ValueError("Wrong effect")
    payload = SCHEMAS[intent.effect].model_validate(raw.get("payload")).model_dump()
    for key in ("to", "path", "query", "title"):
        if key in payload and payload[key] != getattr(intent, key):
            raise ValueError("Result does not bind to requested arguments")
    if intent.effect == "message-delivered" and payload["body_sha256"] != digest(intent.body):
        raise ValueError("Wrong message body receipt")
    if intent.effect == "file-written" and (payload["bytes_written"] != len(intent.content.encode("utf-8")) or
                                          payload["content_sha256"] != digest(intent.content)):
        raise ValueError("Wrong write receipt")
    if intent.effect == "ticket-created" and payload["detail_sha256"] != digest(intent.detail):
        raise ValueError("Wrong ticket receipt")
    return payload
