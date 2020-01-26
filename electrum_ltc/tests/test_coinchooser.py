from electrum_ltc.coinchooser import CoinChooserPrivacy
from electrum_ltc.util import NotEnoughFunds

from . import ElectrumTestCase


class TestCoinChooser(ElectrumTestCase):

    def test_bucket_candidates_with_empty_buckets(self):
        def sufficient_funds(buckets, *, bucket_value_sum):
            return True
        coin_chooser = CoinChooserPrivacy()
        self.assertEqual([[]], coin_chooser.bucket_candidates_any([], sufficient_funds))
        self.assertEqual([[]], coin_chooser.bucket_candidates_prefer_confirmed([], sufficient_funds))
        def sufficient_funds(buckets, *, bucket_value_sum):
            return False
        with self.assertRaises(NotEnoughFunds):
            coin_chooser.bucket_candidates_any([], sufficient_funds)
        with self.assertRaises(NotEnoughFunds):
            coin_chooser.bucket_candidates_prefer_confirmed([], sufficient_funds)
