"""The one function that writes to audit_log.

"Things get easily lost" was the founding complaint about ClickUp — every
mutating action should leave a real answer to "who changed this and when."
Called from route handlers after a successful write, inside the same
request (and therefore the same transaction, committed together by
app.py's teardown_request).
"""
import json


def record(conn, viewer, entity_type: str, entity_id: int, action: str, detail: dict | None = None):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO audit_log (person_id, entity_type, entity_id, action, detail)
           VALUES (%s, %s, %s, %s, %s)""",
        (viewer.id if viewer else None, entity_type, entity_id, action,
         json.dumps(detail) if detail is not None else None),
    )
