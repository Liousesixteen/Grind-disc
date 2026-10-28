"""Exercise the real read routes without opening network sockets."""
import io
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from intraday_board import make_handler


class BoardRoutes(unittest.TestCase):
    def request(self, path):
        class Board:
            def get_state(self): return {'quote_date': '2026-10-27'}
            def watches_today(self): return [{'code': '000001', 'entry_price': 10}]
        h = make_handler(Board(), page_loader=lambda: '<html>preview</html>')
        obj = h.__new__(h)
        obj.path, obj.wfile, obj.headers_seen = path, io.BytesIO(), {}
        obj.send_response = lambda status: setattr(obj, 'status', status)
        obj.send_header = lambda key, value: obj.headers_seen.update({key: value})
        obj.end_headers = lambda: None
        obj.do_GET()
        return obj

    def test_watches_are_readable_and_not_cacheable(self):
        r = self.request('/api/watches?ts=1')
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(r.wfile.getvalue())['records'][0]['entry_price'], 10)
        self.assertEqual(r.headers_seen['Cache-Control'], 'no-store')

    def test_state_route_remains_compatible(self):
        r = self.request('/api/state?ts=1')
        self.assertEqual(json.loads(r.wfile.getvalue())['quote_date'], '2026-10-27')
        self.assertEqual(r.headers_seen['Cache-Control'], 'no-store')

    def test_template_and_unknown_route(self):
        self.assertEqual(self.request('/').wfile.getvalue(), b'<html>preview</html>')
        self.assertEqual(self.request('/missing').status, 404)
