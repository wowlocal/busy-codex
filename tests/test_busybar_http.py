import io
import json
import unittest
from unittest import mock
import urllib.error

from busybar_http import HttpTransport, local_opener, SourceAddressHTTPHandler


class HttpTransportTest(unittest.TestCase):
    def test_busy_screen_is_reachable_and_a_write_is_never_retried(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            'http://device/api/display/draw', 409, 'Busy', {}, io.BytesIO())
        transport = HttpTransport('http://device/api', opener=opener)
        self.assertFalse(transport.draw({'application_name': 'test', 'elements': []}))
        self.assertTrue(transport.device_ok)
        self.assertEqual(409, transport.last_http_status)
        self.assertEqual(1, opener.open.call_count)
        request = opener.open.call_args.args[0]
        self.assertEqual('test', json.loads(request.data)['application_name'])
        self.assertEqual(2.0, opener.open.call_args.kwargs['timeout'])

    def test_reconnection_is_logged_once_and_clear_is_scoped(self):
        opener, logger = mock.MagicMock(), mock.Mock()
        transport = HttpTransport('http://device/api', opener=opener, logger=logger)
        opener.open.side_effect = OSError('unplugged')
        transport.clear('busy codex')
        transport.clear('busy codex')
        self.assertFalse(transport.device_ok)
        self.assertEqual(1, logger.call_count)
        opener.open.side_effect = None
        opener.open.return_value.__enter__.return_value.status = 200
        self.assertTrue(transport.clear('busy codex'))
        self.assertTrue(transport.device_ok)
        self.assertEqual(2, logger.call_count)
        self.assertEqual('http://device/api/display/draw?application_name=busy%20codex',
                         opener.open.call_args.args[0].full_url)

    def test_usb_binding_is_optional_and_proxies_are_disabled(self):
        usb = local_opener('10.0.4.21')
        handlers = [handler for handler in usb.handlers
                    if isinstance(handler, SourceAddressHTTPHandler)]
        self.assertEqual(['10.0.4.21'], [handler.source_address for handler in handlers])
        self.assertFalse(any(isinstance(handler, SourceAddressHTTPHandler)
                             for handler in local_opener().handlers))


if __name__ == '__main__':
    unittest.main()
