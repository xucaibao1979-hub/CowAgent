import unittest
from unittest.mock import patch

from intelligence_center.safe_network import UnsafeTargetError, validate_outbound_url


class SafeNetworkTest(unittest.TestCase):
    def test_blocks_local_private_and_credentials(self):
        for url in ("http://localhost/admin", "http://127.0.0.1/", "http://169.254.169.254/latest", "http://user:pass@example.com/"):
            with self.subTest(url=url), self.assertRaises(UnsafeTargetError):
                validate_outbound_url(url)

    @patch("intelligence_center.safe_network.socket.getaddrinfo")
    def test_allows_public_address_and_can_explicitly_allow_private(self, resolver):
        resolver.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        self.assertEqual(validate_outbound_url("https://example.com/feed"), "https://example.com/feed")
        self.assertEqual(validate_outbound_url("http://localhost/hook", allow_private=True), "http://localhost/hook")


if __name__ == "__main__":
    unittest.main()
