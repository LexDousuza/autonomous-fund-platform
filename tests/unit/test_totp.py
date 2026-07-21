"""
Unit tests for core.utils.totp, validated against RFC 6238's official
test vectors (Appendix B) - not just self-consistency.
"""
from __future__ import annotations

import base64
import unittest

from core.utils.totp import generate_totp

# RFC 6238 Appendix B uses the ASCII secret "12345678901234567890" (20
# bytes, SHA-1 case) - base32-encode it to feed our function, which
# expects a base32 seed (matching what Groww's QR code / dashboard
# actually hands out).
_RFC6238_SHA1_SECRET_B32 = base64.b32encode(b"12345678901234567890").decode()


class GenerateTotpRfc6238VectorsTests(unittest.TestCase):
    """
    Table from RFC 6238 Appendix B (SHA-1 rows only - our implementation
    is SHA-1 only, matching Google Authenticator/Groww's QR default):
    Time(sec) | UTC time            | TOTP
    59        | 1970-01-01 00:00:59 | 94287082
    1111111109| 2005-03-18 01:58:29 | 07081804
    1111111111| 2005-03-18 01:58:31 | 14050471
    1234567890| 2009-02-13 23:31:30 | 89005924
    2000000000| 2033-05-18 03:33:20 | 69279037
    RFC vectors are 8-digit; we compare the last 6 digits since Groww
    (like most real-world TOTP consumers) uses 6-digit codes and 6
    digits is just a shorter truncation of the same computation.
    """

    def test_known_vectors(self) -> None:
        vectors = [
            (59, "94287082"),
            (1111111109, "07081804"),
            (1111111111, "14050471"),
            (1234567890, "89005924"),
            (2000000000, "69279037"),
        ]
        for timestamp, expected_8_digit in vectors:
            with self.subTest(timestamp=timestamp):
                code = generate_totp(_RFC6238_SHA1_SECRET_B32, digits=8, timestamp=timestamp)
                self.assertEqual(code, expected_8_digit)

    def test_default_is_six_digits(self) -> None:
        code = generate_totp(_RFC6238_SHA1_SECRET_B32, timestamp=59)
        self.assertEqual(len(code), 6)
        self.assertEqual(code, "287082")  # last 6 of the known 8-digit vector "94287082"

    def test_same_time_window_gives_same_code(self) -> None:
        code_a = generate_totp(_RFC6238_SHA1_SECRET_B32, timestamp=100)
        code_b = generate_totp(_RFC6238_SHA1_SECRET_B32, timestamp=115)  # same 30s window
        self.assertEqual(code_a, code_b)

    def test_next_time_window_gives_different_code(self) -> None:
        code_a = generate_totp(_RFC6238_SHA1_SECRET_B32, timestamp=100)
        code_b = generate_totp(_RFC6238_SHA1_SECRET_B32, timestamp=131)  # next 30s window
        self.assertNotEqual(code_a, code_b)

    def test_accepts_lowercase_and_whitespace_padded_secret(self) -> None:
        messy_secret = f"  {_RFC6238_SHA1_SECRET_B32.lower()}  "
        self.assertEqual(
            generate_totp(messy_secret, timestamp=59),
            generate_totp(_RFC6238_SHA1_SECRET_B32, timestamp=59),
        )


if __name__ == "__main__":
    unittest.main()
