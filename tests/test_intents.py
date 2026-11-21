import pytest
from pydantic import ValidationError
from backend.intents import SendMessage, ReadFile, WriteFile, LookupRecord, CreateTicket, INTENT_SCHEMA_VERSION

def test_intent_version():
    sm = SendMessage(to="a@b.com", body="hello")
    assert sm.schema_version == INTENT_SCHEMA_VERSION
    assert sm.effect == "message-delivered"

def test_send_message_validation():
    with pytest.raises(ValidationError):
        SendMessage(to="a" * 255, body="hello")
    with pytest.raises(ValidationError):
        SendMessage(to="a@b.c", body="b" * 2001)

def test_file_paths_validation():
    # Valid
    ReadFile(path="valid_file.txt")
    ReadFile(path="some/nested/file.csv")
    WriteFile(path="out.json", content="data")

    # Invalid characters / path traversal attempts
    with pytest.raises(ValidationError):
        ReadFile(path="../etc/passwd")
    
    with pytest.raises(ValidationError):
        ReadFile(path=";rm -rf /")

    with pytest.raises(ValidationError):
        WriteFile(path="valid.txt", content="data", effect="wrong-effect") # strict literal
    for path in ("/etc/passwd", "a//b", "a/./b", "a/../b", "a/"):
        with pytest.raises(ValidationError):
            ReadFile(path=path)
    with pytest.raises(ValidationError):
        WriteFile(path="out.txt", content="x" * 16001)
    with pytest.raises(ValidationError):
        LookupRecord(query="q", handler_name="hidden")

def test_lookup_and_ticket():
    LookupRecord(query="select something")
    with pytest.raises(ValidationError):
        LookupRecord(query="q" * 501)
    
    CreateTicket(title="Bug", detail="Details")
    with pytest.raises(ValidationError):
        CreateTicket(title="B" * 121, detail="Details")
    with pytest.raises(ValidationError):
        CreateTicket(title="Bug", detail="D" * 2001)
