"""One-off CLI for creating a login or resetting a password.

There is no self-signup and no HTTP endpoint for this by design (CLAUDE.md:
owner creates accounts). Run wherever DATABASE_URL is available — locally
through `railway connect postgres --tunnel-only`, or directly on Railway.

    python scripts/create_person.py --name "Broc Curry" --email broccurry@gmail.com --access owner
    python scripts/create_person.py --reset-password --email cody@innovationconcerts.com
"""
import argparse
import sys

sys.path.insert(0, ".")  # allow running from the repo root
import auth
import db


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name")
    p.add_argument("--email", required=True)
    p.add_argument("--access", choices=["crew", "booker", "owner"], default="crew")
    p.add_argument("--phone")
    p.add_argument("--reset-password", action="store_true")
    args = p.parse_args()

    conn = db.get_connection()
    cur = conn.cursor()
    temp_password = auth.generate_temp_password()
    password_hash = auth.hash_password(temp_password)

    if args.reset_password:
        cur.execute(
            "UPDATE people SET password_hash = %s, updated_at = now() "
            "WHERE lower(email) = lower(%s) RETURNING id, name",
            (password_hash, args.email),
        )
        row = cur.fetchone()
        if row is None:
            print(f"No person found with email {args.email}", file=sys.stderr)
            sys.exit(1)
        conn.commit()
        print(f"Temp password for {row[1]} <{args.email}>: {temp_password}")
        print("Log in with it, then call POST /api/change-password.")
        return

    if not args.name:
        print("--name is required when creating a new person", file=sys.stderr)
        sys.exit(1)

    cur.execute(
        """INSERT INTO people (name, email, password_hash, access_level, phone)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (args.name, args.email, password_hash, args.access, args.phone),
    )
    person_id = cur.fetchone()[0]
    conn.commit()
    print(f"Created person id={person_id}, access={args.access}")
    print(f"Temp password: {temp_password}")


if __name__ == "__main__":
    main()
