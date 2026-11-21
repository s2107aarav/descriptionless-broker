"""Closed, bounded effect schema. Model output cannot carry execution metadata."""
import uuid
from pathlib import PurePosixPath
from typing import Literal, Union, Annotated
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

INTENT_SCHEMA_VERSION = "v1"


class BaseIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    schema_version: Literal["v1"] = "v1"


class SendMessage(BaseIntent):
    effect: Literal["message-delivered"] = "message-delivered"
    to: str = Field(min_length=1, max_length=254, pattern=r"^[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
    body: str = Field(min_length=1, max_length=2000)


class FileIntent(BaseIntent):
    path: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9/_.-]+$")

    @field_validator("path")
    @classmethod
    def relative_canonical_path(cls, value):
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "." in value.split("/") or str(path) != value:
            raise ValueError("Only canonical relative paths are accepted")
        return value


class ReadFile(FileIntent):
    effect: Literal["file-content-returned"] = "file-content-returned"


class WriteFile(FileIntent):
    effect: Literal["file-written"] = "file-written"
    content: str = Field(max_length=16000)


class LookupRecord(BaseIntent):
    effect: Literal["record-returned"] = "record-returned"
    query: str = Field(min_length=1, max_length=500)


class CreateTicket(BaseIntent):
    effect: Literal["ticket-created"] = "ticket-created"
    title: str = Field(min_length=1, max_length=120)
    detail: str = Field(min_length=1, max_length=2000)


Intent = Annotated[Union[SendMessage, ReadFile, WriteFile, LookupRecord, CreateTicket], Field(discriminator="effect")]
INTENT_ADAPTER = TypeAdapter(Intent)
INTENT_TYPES = (SendMessage, ReadFile, WriteFile, LookupRecord, CreateTicket)
