"""Daily Etix ticket-count snapshot — run on a schedule (a Railway Cron
Schedule pointed at this script, separate from the always-on web
service), not part of the web process.

Etix has no endpoint that returns a retroactive day-by-day ticket COUNT
history (only day-by-day revenue is retained on their side) — a real
count history has to be built by calling their live snapshot repeatedly
over time and storing it ourselves. This is that repeated call: once a
day, for every confirmed, not-yet-passed show, it captures today's
cumulative ticket count into ticket_sales (source='etix'). Uses the same
etix.pull_and_store_snapshot as the show editor's manual "Pull from
Etix" button, so the two can't disagree about how a count got there.

    python scripts/pull_etix_daily_sales.py
"""
import sys
from datetime import date

sys.path.insert(0, ".")  # allow running from the repo root
import db
import etix


def main():
    conn = db.get_connection()
    cur = conn.cursor()
    today = date.today()
    cur.execute(
        """SELECT e.id, e.show_date, v.name FROM events e JOIN venues v ON v.id = e.venue_id
           WHERE e.status = 'confirmed' AND e.show_date >= %s""",
        (today,),
    )
    rows = cur.fetchall()
    pulled = 0
    for event_id, show_date, venue_name in rows:
        try:
            result = etix.pull_and_store_snapshot(conn, event_id, venue_name, show_date, sale_date=today)
        except Exception as e:
            print(f"event {event_id} ({venue_name}, {show_date}): {e}", file=sys.stderr)
            continue
        if result is not None:
            pulled += 1
    conn.commit()
    print(f"Pulled Etix snapshots for {pulled}/{len(rows)} upcoming confirmed shows.")
    conn.close()


if __name__ == "__main__":
    main()
