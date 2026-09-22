# ConcertPros

A booking calendar + venue management app for Innovation Concerts (3 venues: Frankies, Ottawa Tavern, Cla-Zel Theater). Deliberately separate from SellHQ — different business, different repo, different Railway project, no shared database or credentials.

Full design history and decisions live in this Claude Code project's memory (`project_concertpros.md`) — read it before starting work each session. It covers: why off-the-shelf tools (Prism, Opendate, Muzeek) were rejected, the ClickUp audit findings, the Airtable-prototype-turned-artifact, and the concertpro.live build plan.

## The core problem this app exists to solve

Broc's booking team (Cody, Christian) uses ClickUp today. It "works" for them but permits the same fact to be recorded in more than one way — a show's venue lives in both which list it's filed under AND a tag, and those two disagree on some shows. The result: Broc, the owner, has no reliable view of his own business.

**The fix is structural, not procedural.** Every fact must have exactly one place it can be recorded. This is the single most important design constraint in this codebase — more important than any individual feature.

## Non-negotiable rules

1. **One function decides permissions.** All row-level access control (what a `crew` user vs a `booker` vs the `owner` can see) goes through one function, e.g. `events_for(viewer)`, that every endpoint calls. Filter in SQL, never fetch-then-hide in application code or in the client. A crew session must be structurally unable to receive a hold, a deal, a price, or a settlement — not merely have those fields hidden by the UI. Write a test that asserts this.

2. **No duplicated decisions.** If the same piece of logic (a status transition, a permission check, a formatting rule) needs to exist in two places, that's a bug waiting to happen — collapse it into one shared function instead. Before calling anything "done," grep for every other place that makes the same decision and check each one.

3. **Verify, don't assume.** A deploy succeeding is not the same as the feature working. Check real output — a real HTTP response, a real query result — before saying something works. State plainly what was checked and what wasn't.

4. **Never put a machine-specific path in a hook or config file.** Use `$HOME` or `${CLAUDE_PROJECT_DIR}`, never `C:\Users\<name>\...` literally — this repo is used from more than one Windows machine (`C:\Users\User` and `C:\Users\takin`).

5. **Backups are phase one.** This app will hold Broc's live booking calendar. Don't defer backup/export until "later."

6. **Phone-first for crew, not an afterthought.** 15 of ~17 users only ever open this on a phone, standing in a venue. The crew view is designed for that first; a desktop-shrunk layout is not acceptable there. Bookers (Cody, Christian) also book from their phones mid-call — the "add a hold" flow must be fast enough to use one-handed while talking.

## Repository / infrastructure

- This repo lives at `C:\Users\takin\concertpros`, **outside OneDrive on purpose** — SellHQ's history has two separate incidents where OneDrive syncing a git pointer file silently broke backups across machines. GitHub is the sync mechanism between machines here, not OneDrive.
- Deploys to its own Railway **project** (not a service inside the SellHQ project) — no shared private network, no shared credentials.
- Domain: `concertpro.live` — point **both the apex and `www`** at Railway from day one (SellHQ/Cla-Zel learned this the hard way: an apex-only setup silently 404s).
- Auth: email + password (not a PIN — the PIN in SellHQ exists because a *register device* is shared; here every user has their own phone, so the session already identifies the person).

## Working agreements

See this project's `user_profile.md` in Claude Code memory. Short version: say what's verified vs assumed, ask plainly rather than with a multiple-choice menu, and flag real cost/scope changes as they're discovered rather than at the end.
