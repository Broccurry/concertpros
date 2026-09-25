"""Pure math, no DB -- the settlement calculation engine, checked against
Broc's own real examples and his historical offer/settlement sheets
(2026-09-25) rather than invented numbers.
"""
import unittest

import settlement_calc as calc


class NetGross(unittest.TestCase):
    def test_deducts_tax_facility_and_ticketing_fee_before_expenses(self):
        net_gross, breakdown = calc.compute_net_gross(
            gross=10000, sales_tax_rate=6.75, facility_fee_per_ticket=1.5, tickets_sold=200,
            ticketing_fee_rate=4,
        )
        self.assertAlmostEqual(breakdown["sales_tax"], 675.0)
        self.assertAlmostEqual(breakdown["facility_fee"], 300.0)
        self.assertAlmostEqual(breakdown["ticketing_fee"], 400.0)
        self.assertAlmostEqual(net_gross, 10000 - 675 - 300 - 400)

    def test_zero_gross_is_zero_net_not_an_error(self):
        net_gross, _ = calc.compute_net_gross(gross=0)
        self.assertEqual(net_gross, 0)


class ArtistPayout(unittest.TestCase):
    def test_flat_guarantee_ignores_everything_else(self):
        payout = calc.compute_artist_payout(
            calc.DEAL_GUARANTEE, guarantee=1000, backend_pct=999, net_gross=1, net_after_expenses=-500,
        )
        self.assertEqual(payout, 1000)

    def test_guarantee_vs_pct_takes_the_guarantee_when_bigger(self):
        """Broc's own example: $1000 vs 70%, whichever's greater."""
        payout = calc.compute_artist_payout(
            calc.DEAL_GUARANTEE_VS_PCT, guarantee=1000, backend_pct=70,
            net_gross=1200, net_after_expenses=1200,
        )
        # 70% of 1200 = 840, less than the 1000 guarantee
        self.assertEqual(payout, 1000)

    def test_guarantee_vs_pct_takes_the_percentage_when_bigger(self):
        payout = calc.compute_artist_payout(
            calc.DEAL_GUARANTEE_VS_PCT, guarantee=1000, backend_pct=70,
            net_gross=3000, net_after_expenses=3000,
        )
        # 70% of 3000 = 2100, more than the 1000 guarantee
        self.assertEqual(payout, 2100)

    def test_guarantee_plus_pct_worked_example(self):
        """$500 guarantee, 20% backend, $1000 expenses, $3000 gross ->
        overage = 3000 - 1000 - 500 = 1500 -> bonus = 300 -> payout = 800."""
        payout = calc.compute_artist_payout(
            calc.DEAL_GUARANTEE_PLUS_PCT, guarantee=500, backend_pct=20,
            net_gross=3000, net_after_expenses=3000 - 1000,
        )
        self.assertAlmostEqual(payout, 800)

    def test_guarantee_plus_pct_second_worked_example(self):
        """Broc's second example: $1000 guarantee, $2000 additional
        expenses -> $3000 combined breakeven -> band gets the % of
        whatever clears $3000, on top of the $1000."""
        net_gross = 5000
        net_after_expenses = net_gross - 2000  # expenses alone, guarantee not included here
        payout = calc.compute_artist_payout(
            calc.DEAL_GUARANTEE_PLUS_PCT, guarantee=1000, backend_pct=50,
            net_gross=net_gross, net_after_expenses=net_after_expenses,
        )
        # overage above the $3000 combined breakeven = 5000 - 3000 = 2000; bonus = 50% * 2000 = 1000
        self.assertAlmostEqual(payout, 1000 + 1000)

    def test_guarantee_plus_pct_never_pays_below_the_guarantee(self):
        """A bad night (net after expenses doesn't even clear the
        guarantee) still guarantees the flat amount -- no negative bonus."""
        payout = calc.compute_artist_payout(
            calc.DEAL_GUARANTEE_PLUS_PCT, guarantee=1000, backend_pct=20,
            net_gross=500, net_after_expenses=500,
        )
        self.assertEqual(payout, 1000)

    def test_door_split_above_expenses(self):
        payout = calc.compute_artist_payout(
            calc.DEAL_DOOR_SPLIT, guarantee=0, backend_pct=15,
            net_gross=5000, net_after_expenses=2000, door_split_from_dollar_one=False,
        )
        self.assertAlmostEqual(payout, 300)  # 15% of 2000

    def test_door_split_from_dollar_one_uses_net_gross_not_after_expenses(self):
        payout = calc.compute_artist_payout(
            calc.DEAL_DOOR_SPLIT, guarantee=0, backend_pct=15,
            net_gross=5000, net_after_expenses=2000, door_split_from_dollar_one=True,
        )
        self.assertAlmostEqual(payout, 750)  # 15% of 5000, not 2000

    def test_door_split_never_goes_negative_on_a_losing_night(self):
        payout = calc.compute_artist_payout(
            calc.DEAL_DOOR_SPLIT, guarantee=0, backend_pct=15,
            net_gross=500, net_after_expenses=-1500,
        )
        self.assertEqual(payout, 0)

    def test_flat_rental_and_promo_and_not_recorded_have_no_payout(self):
        for deal_type in (calc.DEAL_FLAT_RENTAL, calc.DEAL_FREE_PROMO, calc.DEAL_NOT_RECORDED):
            payout = calc.compute_artist_payout(
                deal_type, guarantee=99999, backend_pct=99, net_gross=99999, net_after_expenses=99999,
            )
            self.assertEqual(payout, 0, deal_type)


if __name__ == "__main__":
    unittest.main()
