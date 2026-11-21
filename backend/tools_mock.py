"""Local receipt simulators. None sends mail, accesses files, DBs, or Jira."""
from .contracts import digest


def _mock_result(effect, payload, **metadata):
    return {"effect": effect, "payload": payload, "endpoint": "mock://local",
            "provider": "MockProvider", **metadata}


def smtp_send_a(to, body):
    return _mock_result("message-delivered", {"status": "sent", "to": to, "body_sha256": digest(body)}, handler_name="smtp_send_a")


def fs_read_a(path):
    return _mock_result("file-content-returned", {"path": path, "content": f"mock content of {path}"}, handler_name="fs_read_a")


def fs_write_a(path, content):
    return _mock_result("file-written", {"bytes_written": len(content.encode("utf-8")), "path": path,
                                       "content_sha256": digest(content)}, handler_name="fs_write_a")


def db_lookup_a(query):
    return _mock_result("record-returned", {"query": query, "results": [f"record for {query}"]}, handler_name="db_lookup_a")


def jira_ticket_a(title, detail):
    return _mock_result("ticket-created", {"ticket_id": "MOCK-" + digest(title + detail)[:12],
                                         "title": title, "detail_sha256": digest(detail)}, handler_name="jira_ticket_a")


def _poison(result):
    result["payload"]["marker"] = "POISONED_IMPLEMENTATION"
    return result


def smtp_send_b(to, body): return _poison(smtp_send_a(to, body))
def fs_read_b(path): return _poison(fs_read_a(path))
def fs_write_b(path, content): return _poison(fs_write_a(path, content))
def db_lookup_b(query): return _poison(db_lookup_a(query))
def jira_ticket_b(title, detail): return _poison(jira_ticket_a(title, detail))
