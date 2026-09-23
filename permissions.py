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

    def can_manage_access_level(self, target_level: str) -> bool:
        """A booker may create/edit/reset-password only for 'crew' people —
        never for another booker or the owner, and never grant 'booker' or
        'owner' to anyone. Only the owner can touch people at booker/owner
        rank. One rule, called from every people-admin endpoint, so a
        booker can never elevate themselves or anyone else."""
        if self.access_level == "owner":
            return True
        return target_level == "crew"

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
        SELECT e.id, v.name AS venue, e.show_date, e.doors, e.show_time
        FROM events e
        JOIN venues v ON v.id = e.venue_id
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
    artists_by_event = _artists_by_event(conn, ids)

    # The viewer's own assignment(s) only — never who else is working.
    # Includes the assignment's own id and clock times so the crew clock-
    # in/out button (POST /api/assignments/<id>/clock) has something to
    # act on, scoped to exactly the one row that belongs to them.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asg.event_id, asg.id, r.name AS role, asg.scheduled_time,
                   asg.clocked_in_at, asg.clocked_out_at
            FROM assignments asg
            JOIN roles r ON r.id = asg.role_id
            WHERE asg.person_id = %(person_id)s AND asg.event_id = ANY(%(ids)s)
            """,
            {"person_id": viewer.id, "ids": ids},
        )
        my_roles_by_event: dict[int, list[str]] = {}
        my_assignments_by_event: dict[int, list[dict]] = {}
        for event_id, assignment_id, role, scheduled_time, clocked_in_at, clocked_out_at in cur.fetchall():
            my_roles_by_event.setdefault(event_id, []).append(role)
            my_assignments_by_event.setdefault(event_id, []).append({
                "id": assignment_id, "role": role, "scheduled_time": scheduled_time,
                "clocked_in_at": clocked_in_at, "clocked_out_at": clocked_out_at,
            })

    for e in events:
        e["artists"] = artists_by_event.get(e["id"], [])
        e["my_roles"] = my_roles_by_event.get(e["id"], [])
        e["my_assignments"] = my_assignments_by_event.get(e["id"], [])
    return events


def _artists_by_event(conn, ids: list[int], include_money: bool = False) -> dict[int, list[dict]]:
    """A show's bill, in running order — see the event_artists table
    comment in schema.sql. Shared by both the crew and booker branches so
    there's exactly one query deciding what an act on a bill looks like.
    guarantee/paid/walkups are money, same boundary as guarantee/settlement
    on the event itself — crew's call never selects those columns at all,
    rather than fetching and hiding them. The contact log is booker/owner
    only for the same reason it exists at all (which booker already
    reached out) — crew has no path to it either way."""
    money_cols = ", ea.guarantee, ea.paid, ea.walkups" if include_money else ""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT ea.event_id, ea.id, ea.artist_id, a.name, ea.confirmed, ea.declined, "
            f"ea.notes, ea.bill_role, ea.set_time{money_cols} FROM event_artists ea "
            "JOIN artists a ON a.id = ea.artist_id "
            "WHERE ea.event_id = ANY(%(ids)s) ORDER BY ea.sort_order",
            {"ids": ids},
        )
        by_event: dict[int, list[dict]] = {}
        cols = [c.name for c in cur.description]
        act_ids = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            act_ids.append(d["id"])
            by_event.setdefault(d.pop("event_id"), []).append(d)

    if include_money and act_ids:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.event_artist_id, c.id, c.method, c.created_at, c.note, p.name AS person_name "
                "FROM event_artist_contacts c LEFT JOIN people p ON p.id = c.person_id "
                "WHERE c.event_artist_id = ANY(%(ids)s) ORDER BY c.created_at",
                {"ids": act_ids},
            )
            ccols = [c.name for c in cur.description]
            contacts_by_act: dict[int, list[dict]] = {}
            for row in cur.fetchall():
                d = dict(zip(ccols, row))
                contacts_by_act.setdefault(d.pop("event_artist_id"), []).append(d)
        for acts in by_event.values():
            for a in acts:
                a["contacts"] = contacts_by_act.get(a["id"], [])

    return by_event


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
        SELECT e.id, e.venue_id, v.name AS venue,
               e.show_date, e.doors, e.show_time, e.status,
               e.deal_type, e.guarantee, e.backend_pct, e.deal_notes,
               e.announce_date, e.onsale_date, e.notes, e.ticket_link, e.version, e.hold_group_id
        FROM events e
        JOIN venues v ON v.id = e.venue_id
        WHERE {' AND '.join(where)}
        ORDER BY e.show_date, e.doors
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [c.name for c in cur.description]
        events = [dict(zip(cols, row)) for row in cur.fetchall()]

    if not events:
        return events

    # A date inside an active multi-day hold doesn't own its own bill/
    # deal/tasks/etc — it borrows them from whichever date in its group
    # was created first (see _group_anchor_id in app.py, same rule).
    # effective_id is what every shared-data lookup below keys off of.
    group_ids = {e["hold_group_id"] for e in events if e["hold_group_id"]}
    anchor_by_group: dict[int, int] = {}
    members_by_group: dict[int, list[dict]] = {}
    if group_ids:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT e.hold_group_id, e.id, e.show_date, e.status, e.version, e.venue_id, v.name AS venue "
                "FROM events e JOIN venues v ON v.id = e.venue_id "
                "WHERE e.hold_group_id = ANY(%(gids)s) ORDER BY e.show_date",
                {"gids": list(group_ids)},
            )
            for gid, eid, show_date, status, version, venue_id, venue in cur.fetchall():
                members_by_group.setdefault(gid, []).append(
                    {"id": eid, "show_date": show_date, "status": status, "version": version,
                     "venue_id": venue_id, "venue": venue})
        for gid, members in members_by_group.items():
            anchor_by_group[gid] = min(m["id"] for m in members)

    def effective_id(e):
        return anchor_by_group.get(e["hold_group_id"], e["id"]) if e["hold_group_id"] else e["id"]

    ids = list({effective_id(e) for e in events})
    artists_by_event = _artists_by_event(conn, ids, include_money=True)

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
            "SELECT id, event_id, label, price, sort_order FROM ticket_tiers "
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
            "SELECT id, event_id, label, done, owner_person_id, sort_order FROM event_tasks "
            "WHERE event_id = ANY(%(ids)s) ORDER BY sort_order",
            {"ids": ids},
        )
        tasks_by_event: dict[int, list[dict]] = {}
        cols = [c.name for c in cur.description]
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            tasks_by_event.setdefault(d["event_id"], []).append(d)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asg.id, asg.event_id, r.name AS role, p.id AS person_id, p.name AS person_name,
                   asg.scheduled_time, asg.clocked_in_at, asg.clocked_out_at
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
        eid = effective_id(e)
        e["artists"] = artists_by_event.get(eid, [])
        e["settlement"] = settlements.get(eid)
        e["ticket_tiers"] = tiers_by_event.get(eid, [])
        e["staff"] = staff_by_event.get(eid, [])
        e["tasks"] = tasks_by_event.get(eid, [])
        e["group_members"] = members_by_group.get(e["hold_group_id"], []) if e["hold_group_id"] else []
    return events


def todos_for(conn, viewer: Viewer) -> list[dict]:
    """General ops to-dos, not tied to a show. Crew sees and can act on
    only the ones assigned to them — never anyone else's — booker/owner
    see and assign all of them. Same shape as events_for: the filter runs
    in SQL, not by fetching everything and hiding rows client-side."""
    where = "" if not viewer.is_crew else "WHERE t.assigned_to = %(person_id)s"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT t.id, t.title, t.done, t.assigned_to, p.name AS assigned_to_name,
                   t.due_date, t.notes, t.created_by, t.created_at
            FROM todos t
            LEFT JOIN people p ON p.id = t.assigned_to
            {where}
            ORDER BY t.done, t.due_date NULLS LAST, t.created_at
            """,
            {"person_id": viewer.id},
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
