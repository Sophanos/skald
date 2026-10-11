"""Hydra and OpenRouter transport checks without sockets or real credentials."""
from contextlib import contextmanager
from email.message import Message
import io
import tempfile
import unittest
import urllib.request
from urllib.response import addinfourl
from unittest.mock import Mock, patch

from decide_kit import close_server, load_server


@contextmanager
def transport(body, redirect=None):
    calls, responses = [], []

    def opened(handler, req):
        calls.append(req)
        headers = Message()
        code = 200
        if redirect and len(calls) == 1:
            headers['Location'] = redirect
            code = 302
        response = addinfourl(io.BytesIO(body), headers, req.full_url, code)
        response.msg = 'synthetic response'
        response.read = Mock(wraps=response.read)
        responses.append(response)
        return response

    with patch.object(urllib.request.HTTPHandler, 'http_open', opened), \
            patch.object(urllib.request.HTTPSHandler, 'https_open', opened):
        yield calls, responses


class OutboundTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.s = load_server(temp.name)
        self.addCleanup(close_server, self.s)

    def test_hydra_refuses_redirect_without_contacting_target(self):
        with transport(b'<rss><title>Channel</title></rss>', 'https://elsewhere.invalid/api') as (calls, _):
            with self.assertRaises(self.s.UnsafeURL):
                self.s.nzb_search(('http://hydra/api', 'synthetic-key'), t='search', q='fixture')
        self.assertEqual(len(calls), 1)

    def test_hydra_stops_oversized_body_at_byte_cap(self):
        with patch.object(self.s, 'CONNECTOR_HTTP_BYTES', 64), \
                transport(b'<rss><title>Channel</title></rss>'.ljust(128)) as (_, responses):
            with self.assertRaises(self.s.ByteCapExceeded):
                self.s.nzb_search(('http://hydra/api', 'synthetic-key'), t='search', q='fixture')
        responses[0].read.assert_called_once_with(65)

    def test_openrouter_refuses_redirect_without_contacting_target(self):
        with transport(b'{}', 'https://elsewhere.invalid/api') as (calls, _):
            with self.assertRaises(self.s.UnsafeURL):
                self.s.openrouter('/api/v1/chat/completions', {}, 17, 'search', 'fixture')
        self.assertEqual(len(calls), 1)

    def test_openrouter_stops_oversized_body_at_byte_cap(self):
        with patch.object(self.s, 'CONNECTOR_HTTP_BYTES', 64), transport(b'{}'.ljust(128)) as (_, responses):
            with self.assertRaises(self.s.ByteCapExceeded):
                self.s.openrouter('/api/v1/chat/completions', {}, 17, 'search', 'fixture')
        responses[0].read.assert_called_once_with(65)

    def test_responses_at_byte_cap_preserve_payloads_and_timeouts(self):
        with patch.object(self.s, 'CONNECTOR_HTTP_BYTES', 64), \
                transport(b'<rss><title>Channel</title><title>Release</title></rss>'.ljust(64)) as (calls, responses):
            self.assertEqual(self.s.nzb_search(('http://hydra/api', 'synthetic-key'), t='search'), ['Release'])
        self.assertEqual(calls[0].timeout, 20)
        responses[0].read.assert_called_once_with(65)
        self.assertTrue(responses[0].closed)
        body = b'{"usage":{"cost":0.01,"total_tokens":7},"choices":[]}'.ljust(64)
        with patch.object(self.s, 'CONNECTOR_HTTP_BYTES', 64), patch.object(self.s, 'OR_KEY', 'synthetic-key'), \
                patch.object(self.s.threading.Thread, 'start'), transport(body) as (calls, responses):
            self.assertEqual(self.s.openrouter('/api/v1/chat/completions', {}, 17, 'search', 'fixture')['choices'], [])
        self.assertEqual(calls[0].full_url, 'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(calls[0].get_header('Authorization'), 'Bearer synthetic-key')
        self.assertEqual(calls[0].timeout, 17)
        responses[0].read.assert_called_once_with(65)
        self.assertTrue(responses[0].closed)
        self.assertEqual(self.s._usage['cost'], 0.01)
        self.assertEqual(self.s._usage['tokens'], 7)


if __name__ == '__main__':
    unittest.main()
