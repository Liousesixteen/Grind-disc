import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from preview_board import Preview


class PreviewQuotes(unittest.TestCase):
    def preview(self, records):
        p = Preview('http://127.0.0.1:6060')
        p.journal.watches_today = Mock(return_value=records)
        return p

    @patch('preview_board.src_snapshot_tencent')
    def test_fetches_all_watched_codes_once_not_just_related_candidates(self, fetch):
        p = self.preview([{'code': '002991'}, {'code': '603278'}, {'code': '603278'}])
        fetch.return_value = [{'code': '002991', 'price': 39, 'quote_time': '20261027140000'},
                              {'code': '603278', 'price': 17, 'quote_time': '20261027140001'}]
        a = p.watch_quotes()
        self.assertEqual(set(a), {'002991', '603278'})
        self.assertEqual(a['603278']['quote_time'], '20261027140001')
        self.assertTrue(a['603278']['fetched_at'])
        self.assertEqual(p.watch_quotes(), a)
        fetch.assert_called_once_with(['002991', '603278'], workers=1)

    @patch('preview_board.src_snapshot_tencent')
    def test_empty_watchlist_does_not_fetch_whole_market(self, fetch):
        self.assertEqual(self.preview([]).watch_quotes(), {})
        fetch.assert_not_called()

    @patch('preview_board.src_snapshot_tencent')
    def test_expiry_updates_price_and_failure_does_not_disguise_old_quote(self, fetch):
        p = self.preview([{'code': '002991'}])
        fetch.return_value = [{'code': '002991', 'price': 39, 'quote_time': '20261027140000'}]
        self.assertEqual(p.watch_quotes()['002991']['price'], 39)
        p.quote_at = 0
        fetch.return_value = [{'code': '002991', 'price': 40, 'quote_time': '20261027140016'}]
        self.assertEqual(p.watch_quotes()['002991']['price'], 40)
        p.quote_at = 0
        fetch.return_value = []
        self.assertEqual(p.watch_quotes(), {})
