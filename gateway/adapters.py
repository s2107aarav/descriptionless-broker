"""Transactional local adapters and closed receipts."""
from dataclasses import dataclass


@dataclass
class Rejected(Exception):
    code: str


class Uncertain(Exception):
    """An effect may have committed. Automatic replay would be unsafe."""


def send_message(db, row, intent, now):
    if not db.execute("SELECT 1 FROM principals WHERE name=? AND scope=? AND active=1",
                      (intent.recipient, row["scope"])).fetchone():
        raise Rejected("recipient_unavailable")
    message_id = "msg-" + row["id"]
    db.execute("INSERT INTO inbox VALUES (?,?,?,?,?,?,?)",
               (message_id, row["scope"], intent.recipient, row["requester"], intent.body, now, row["id"]))
    return {"delivery": "delivered_to_local_inbox", "message_id": message_id, "recipient": intent.recipient}


def find_ticket(db, scope, ticket_id):
    row = db.execute("SELECT * FROM tickets WHERE scope=? AND id=?", (scope, ticket_id)).fetchone()
    if row is None:
        raise Rejected("record_not_found")
    return row


def ticket_receipt(row):
    return {"ticket_id": row["id"], "revision": row["revision"], "status": row["status"]}


def lookup_record(db, row, intent, _now):
    if intent.collection == "services":
        record = db.execute("SELECT id AS record_id,name,description FROM services WHERE scope=? AND id=?",
                            (row["scope"], intent.record_id)).fetchone()
        if record is None:
            raise Rejected("record_not_found")
        payload = dict(record)
    else:
        record = find_ticket(db, row["scope"], intent.record_id)
        payload = {**ticket_receipt(record), "title": record["title"], "detail": record["detail"]}
    return {"collection": intent.collection, "record": payload}


def create_ticket(db, row, intent, _now):
    ticket_id = "tkt-" + row["id"]
    db.execute("""INSERT INTO tickets (id,scope,title,detail,status,revision,origin_execution)
        VALUES (?,?,?,?,'open',1,?)""", (ticket_id, row["scope"], intent.title, intent.detail, row["id"]))
    return ticket_receipt(find_ticket(db, row["scope"], ticket_id))


TICKET_TRANSITIONS = {"open": {"investigating"}, "investigating": {"resolved"}, "resolved": set()}


def update_ticket(db, row, intent, _now):
    current = find_ticket(db, row["scope"], intent.ticket_id)
    if current["revision"] != intent.expected_revision:
        raise Rejected("ticket_revision_conflict")
    if current["revision"] >= 2147483647:
        raise Rejected("ticket_revision_limit")
    status = intent.status if intent.status is not None else current["status"]
    detail = intent.detail if intent.detail is not None else current["detail"]
    if status != current["status"] and status not in TICKET_TRANSITIONS[current["status"]]:
        raise Rejected("invalid_ticket_transition")
    if (status, detail) == (current["status"], current["detail"]):
        raise Rejected("ticket_unchanged")
    change = db.execute("""UPDATE tickets SET status=?,detail=?,revision=revision+1
        WHERE scope=? AND id=? AND revision=?""",
        (status, detail, row["scope"], intent.ticket_id, intent.expected_revision))
    if change.rowcount != 1:
        raise Rejected("ticket_revision_conflict")
    return ticket_receipt(find_ticket(db, row["scope"], intent.ticket_id))


SQL_ADAPTERS = {
    "send-message": send_message,
    "lookup-record": lookup_record,
    "create-ticket": create_ticket,
    "update-ticket": update_ticket,
}
