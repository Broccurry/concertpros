"""The one place that decides what a viewer can see.

CLAUDE.md rule 1: all row-level access control goes through this module.
No endpoint queries `events`, `settlements`, `ticket_tiers`, or `event_tasks`
directly — it calls events_for(conn, viewer, ...) and gets back exactly the
shape that viewer is allowed to have. The crew branch below doesn't select
sensitive columns and doesn't JOIN settlements or ticket_tiers at all —
there is nothing to accidentally leak because it was never fetched.

Access levels (people.access_level): 'crew' | 'booker' | 'owner'.
'booker' and 'owner' see identically full data for booking purposes;
they differ only in whether they can manage people/permissions, enforced
separately in the people/admin endpoints, not here.
"""
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Viewer:
    id: int
    access_level: str  # 'crew' | 'booker' | 'owner'

    @property
    def is_crew(self) -> bool:
        return self.access_level == "crew"


# Statuses a crew viewer may ever see. Holds are booking-in-progress
# information; a dead date never happened. Deliberately a allowlist, not a
# blocklist — a new status added later defaults to invisible to crew until
# someone decides otherwise, rather than defaulting to visible.
_CREW_VISIBLE_STATUSES = ("confirmed", "complete")


def events_for(conn, viewer: Viewer, date_from: date | None = None, date_to: date | None = None,
                venue_id: int | None = None) -> list[dict]:
    """Every field this function does not put in the returned dict is a
    field that viewer cannot see, by construction. Extending what crew can
    see means deliberately adding a column/join here — never something an
    individual endpoint decides on its own.
    """
    if viewer.is_crew:
        return _events_for_crew(conn, viewer, date_from, date_to, venue_id)
    return _events_for_booker(conn, date_from, date_to, venue_id)


def _events_for_crew(conn, viewer: Viewer, date_from, date_to, venue_id) -> list[dict]:
    where = ["e.status = ANY(%(statuses)s)"]
    params = {"statuses": list(_CREW_VISIBLE_STATUSES), "person_id": viewer.id}
    if date_from is not None:
        where.append("e.show_date >= %(date_from)s")
        params["date_from"] = date_from
    if date_to is not None:
        where.append("e.show_date <= %(date_to)s")
        params["date_to"] = date_to
    if venue_id is not None:
        where.append("e.venue_id = %(venue_id)s")
        params["venue_id"] = venue_id

    # Only the columns a crew member is allowed to have. No guarantee,
    # backend_pct, deal_notes, announce_date, onsale_date, notes, and no
    # join to settlements or ticket_tiers.
    sql = f"""
        SELECT e.id, v.name AS venue, a.name AS headliner, e.support,
               e.show_date, e.doors, e.show_time
        FROM events e
        JOIN venues v ON v.id = e.venue_id
        JOIN artists a ON a.id = e.artist_id
        WHERE {' AND '.join(where)}
        ORDER BY e.show_date, e.doors
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [c.name for c in cur.description]
        events = [dict(zip(cols, row)) for row in cur.fetchall()]

    if not events:
        return events

    # The viewer's own assignment(s) only — never who else is working.
    ids = [e["id"] for e in events]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asg.event_id, r.name AS role
            FROM assignments asg
            JOIN roles r ON r.id = asg.role_id
            WHERE asg.person_id = %(person_id)s AND asg.event_id = ANY(%(ids)s)
            """,
            {"person_id": viewer.id, "ids": ids},
        )
        my_roles_by_event: dict[int, list[str]] = {}
        for event_id, role in cur.fetchall():
            my_roles_by_event.setdefault(event_id, []).append(role)

    for e in events:
        e["my_roles"] = my_roles_by_event.get(e["id"], [])
    return events


def _events_for_booker(conn, date_from, date_to, venue_id) -> list[dict]:
    where = ["1=1"]
    params: dict = {}
    if date_from is not None:
        where.append("e.show_date >= %(date_from)s")
        params["date_from"] = date_from
    if date_to is not None:
        where.append("e.show_date <= %(date_to)s")
        params["date_to"] = date_to
    if venue_id is not None:
        where.append("e.venue_id = %(venue_id)s")
        params["venue_id"] = venue_id

    sql = f"""
        SELECT e.id, e.venue_id, v.name AS venue, e.artist_id, a.name AS headliner,
               e.support, e.show_date, e.doors, e.show_time, e.status,
               e.deal_type, e.guarantee, e.backend_pct, e.deal_notes,
               e.announce_date, e.onsale_date, e.notes, e.version
        FROM events e
        JOIN venues v ON v.id = e.venue_id
        JOIN artists a ON a.id = e.artist_id
        WHERE {' AND '.join(where)}
        ORDER BY e.show_date, e.doors
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [c.name for c in cur.description]
        events = [dict(zip(cols, row)) for row in cur.fetchall()]

    if not events:
        return events
    ids = [e["id"] for e in events]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_id, tickets_sold, gross, expenses, artist_payout, settled, notes "
            "FROM settlements WHERE event_id = ANY(%(ids)s)",
            {"ids": ids},
        )
        cols = [c.name for c in cur.description]
        settlements = {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_id, label, price, sort_order FROM ticket_tiers "
            "WHERE event_id = ANY(%(ids)s) ORDER BY sort_order",
            {"ids": ids},
        )
        tiers_by_event: dict[int, list[dict]] = {}
        cols = [c.name for c in cur.description]
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            tiers_by_event.setdefault(d["event_id"], []).append(d)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asg.event_id, r.name AS role, p.id AS person_id, p.name AS person_name
            FROM assignments asg
            JOIN roles r ON r.id = asg.role_id
            LEFT JOIN people p ON p.id = asg.person_id
            WHERE asg.event_id = ANY(%(ids)s)
            """,
            {"ids": ids},
        )
        staff_by_event: dict[int, list[dict]] = {}
        cols = [c.name for c in cur.description]
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            staff_by_event.setdefault(d["event_id"], []).append(d)

    for e in events:
        e["settlement"] = settlements.get(e["id"])
        e["ticket_tiers"] = tiers_by_event.get(e["id"], [])
        e["staff"] = staff_by_event.get(e["id"], [])
    return events
