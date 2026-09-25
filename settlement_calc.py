"""The one place a settlement's numbers get computed -- five real deal
shapes, confirmed against Innovation Concerts' own historical offer and
settlement sheets (2026-09-25) plus Broc's direct examples, not guessed.
Every caller (the live preview while editing, the final settle-and-save)
goes through these same two functions so they can't ever disagree.

Deal types and their payout formula (guarantee/backend_pct are the
existing events.guarantee/backend_pct columns; backend_pct is a plain
percentage number like 20, meaning 20%, matching how it's already
displayed elsewhere in this app -- not a 0-1 fraction):

- "Guarantee": flat fee, no split at all. Turnout doesn't matter.
- "Guarantee vs %": whichever is GREATER of the flat guarantee or a
  percentage of net-after-expenses (Broc's example: $1000 vs 70%,
  whichever's bigger -- matches the old Offer Sheet's own label,
  "Artist Settlement (Greater of % vs Guarantee)").
- "Guarantee + %": the artist gets the guarantee PLUS a bonus
  percentage of whatever's left after BOTH expenses AND the guarantee
  itself are recouped (Broc's own worked example: $1000 guarantee,
  $2000 additional expenses -> $3000 combined breakeven -> band gets
  the % of whatever clears $3000, on top of the $1000).
- "Door split": a straight percentage of revenue, no guarantee at all.
  Two variants selected by door_split_from_dollar_one: from the first
  dollar of net gross, or only on the amount above expenses.
- "Flat rental" / "Free / promo" / "Not recorded": no artist payout at
  all -- a flat rental show is venue income, not a band deal; promo
  shows and undecided deals have nothing to compute yet.
"""

DEAL_GUARANTEE = "Guarantee"
DEAL_GUARANTEE_VS_PCT = "Guarantee vs %"
DEAL_GUARANTEE_PLUS_PCT = "Guarantee + %"
DEAL_DOOR_SPLIT = "Door split"
DEAL_FLAT_RENTAL = "Flat rental"
DEAL_FREE_PROMO = "Free / promo"
DEAL_NOT_RECORDED = "Not recorded"

NO_PAYOUT_DEAL_TYPES = (DEAL_FLAT_RENTAL, DEAL_FREE_PROMO, DEAL_NOT_RECORDED)


def compute_net_gross(gross, sales_tax_rate=0, facility_fee_per_ticket=0, tickets_sold=0, ticketing_fee_rate=0):
    """Matches the real settlement sheet's own deduction chain: gross
    ticket revenue minus sales tax minus a per-ticket facility fee minus
    a ticketing-platform fee, all BEFORE any show expense is deducted.
    Returns (net_gross, breakdown) so a caller can display each
    deduction line, not just the total.

    sales_tax_rate/ticketing_fee_rate are plain percentage numbers (6.75
    meaning 6.75%), same convention as events.backend_pct elsewhere in
    this app -- not 0-1 fractions. facility_fee_per_ticket is a flat
    dollar amount per ticket, not a percentage.

    Every input is coerced to float here -- psycopg hands back NUMERIC
    columns as Decimal, which raises TypeError when mixed with a plain
    float in arithmetic, and callers shouldn't have to know or care
    which of guarantee/gross/etc. came from the database vs. a JSON
    body."""
    gross = float(gross or 0)
    sales_tax = gross * float(sales_tax_rate or 0) / 100
    facility_fee = float(facility_fee_per_ticket or 0) * float(tickets_sold or 0)
    ticketing_fee = gross * float(ticketing_fee_rate or 0) / 100
    net_gross = gross - sales_tax - facility_fee - ticketing_fee
    return net_gross, {"sales_tax": sales_tax, "facility_fee": facility_fee, "ticketing_fee": ticketing_fee}


def compute_artist_payout(deal_type, guarantee, backend_pct, net_gross, net_after_expenses,
                           door_split_from_dollar_one=False):
    """net_gross: revenue after tax/facility/ticketing deductions, before
    show expenses. net_after_expenses: net_gross minus show expenses
    (NOT including the guarantee -- the guarantee is combined with
    expenses separately, only for the deal types where that matters).
    All numeric inputs are coerced to float -- see compute_net_gross for
    why (Decimal from the database vs. float from Python arithmetic)."""
    guarantee = float(guarantee or 0)
    pct = float(backend_pct or 0) / 100
    net_gross = float(net_gross or 0)
    net_after_expenses = float(net_after_expenses or 0)

    if deal_type == DEAL_GUARANTEE:
        return guarantee
    if deal_type == DEAL_GUARANTEE_VS_PCT:
        return max(guarantee, pct * net_after_expenses)
    if deal_type == DEAL_GUARANTEE_PLUS_PCT:
        return guarantee + pct * max(0, net_after_expenses - guarantee)
    if deal_type == DEAL_DOOR_SPLIT:
        base = net_gross if door_split_from_dollar_one else net_after_expenses
        return pct * max(0, base)
    if deal_type in NO_PAYOUT_DEAL_TYPES:
        return 0
    return 0
