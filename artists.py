"""Artist lookup and auto-create.

Booking a band nobody has booked before creates the artist record — the
roster builds itself as a byproduct of booking, same as the artifact
prototype. Matching is name-based (case/whitespace/leading-"The"
insensitive) because two different real acts can share a stage name and a
DB uniqueness constraint would be wrong; a human occasionally merging two
near-duplicate artist rows is an acceptable cost for that.
"""
import re


def _normalize(name: str) -> str:
    n = name.strip().lower()
    n = re.sub(r"^the\s+", "", n)
    n = re.sub(r"[^a-z0-9]", "", n)
    return n


def find_or_create(conn, name: str) -> int:
    """Returns an artist id, creating the artist if no name match exists."""
    name = name.strip()
    if not name:
        raise ValueError("artist name is required")
    target = _normalize(name)

    cur = conn.cursor()
    cur.execute("SELECT id, name FROM artists")
    for artist_id, existing_name in cur.fetchall():
        if _normalize(existing_name) == target:
            # Booking them is unambiguous evidence they're active again —
            # an archived band shouldn't stay hidden from the roster once
            # someone's actually booking them.
            cur.execute("UPDATE artists SET active = TRUE WHERE id = %s AND active = FALSE", (artist_id,))
            return artist_id

    cur.execute("INSERT INTO artists (name) VALUES (%s) RETURNING id", (name,))
    return cur.fetchone()[0]


def list_artists(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, tier, genre, sub_genre, tags, location, instagram, facebook, website, spotify, notes, active "
        "FROM artists ORDER BY name"
    )
    cols = [c.name for c in cur.description]
    artists = [dict(zip(cols, row)) for row in cur.fetchall()]
    if not artists:
        return artists

    ids = [a["id"] for a in artists]
    cur.execute(
        "SELECT id, artist_id, name, phone, email FROM artist_members "
        "WHERE artist_id = ANY(%s) ORDER BY sort_order",
        (ids,),
    )
    members_by_artist: dict[int, list[dict]] = {}
    mcols = [c.name for c in cur.description]
    for row in cur.fetchall():
        d = dict(zip(mcols, row))
        members_by_artist.setdefault(d.pop("artist_id"), []).append(d)
    for a in artists:
        a["members"] = members_by_artist.get(a["id"], [])
    return artists
