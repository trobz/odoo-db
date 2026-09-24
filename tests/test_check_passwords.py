"""Tests for the check-passwords command's pure logic.

Hashes are generated with tiny round counts so the suite stays fast;
the format is identical to what Odoo stores (only the round count differs).
"""

import base64
import hashlib

from odoo_db.db import (
    _COMMON_WEAK_PASSWORDS,
    check_weak_password,
    parse_password_hash,
    weak_password_candidates,
)


def _pbkdf2_hash(password: str, *, salt: bytes = b"testsalt", rounds: int = 1000, scheme: str = "pbkdf2-sha512") -> str:
    algo = "sha512" if scheme == "pbkdf2-sha512" else "sha1"
    digest = hashlib.pbkdf2_hmac(algo, password.encode(), salt, rounds)

    def ab64(b: bytes) -> str:
        return base64.b64encode(b).rstrip(b"=").decode().replace("+", ".")

    return f"${scheme}${rounds}${ab64(salt)}${ab64(digest)}"


def test_parse_password_hash_pbkdf2():
    parsed = parse_password_hash(_pbkdf2_hash("x"))
    assert parsed is not None
    scheme, rounds, salt, _digest = parsed
    assert scheme == "pbkdf2-sha512"
    assert rounds == 1000
    assert salt == b"testsalt"


def test_parse_password_hash_plaintext_and_garbage():
    parsed = parse_password_hash("hello")
    assert parsed is not None and parsed[0] == "plaintext"
    assert parse_password_hash("") is None
    assert parse_password_hash("$argon2$x$y$z") is None
    assert parse_password_hash("$pbkdf2-sha512$notanumber$s$d") is None


def test_weak_password_single_char():
    stored = _pbkdf2_hash("5")
    assert check_weak_password("someone", stored) == "single_char"


def test_weak_password_admin():
    stored = _pbkdf2_hash("admin")
    assert check_weak_password("someone", stored) == "common"


def test_weak_password_same_as_login():
    stored = _pbkdf2_hash("johndoe")
    assert check_weak_password("johndoe", stored) == "same_as_login"
    # case-insensitive
    assert check_weak_password("JohnDoe", _pbkdf2_hash("johndoe")) == "same_as_login"


def test_strong_password_not_flagged():
    stored = _pbkdf2_hash("e$mKv82!pzQw")
    assert check_weak_password("johndoe", stored) is None


def test_plaintext_always_weak():
    assert check_weak_password("johndoe", "johndoe") == "plaintext"
    assert check_weak_password("johndoe", "whatever") == "plaintext"


def test_malformed_rounds_flagged():
    # "A"*86 decodes to exactly 64 bytes (sha512's digest size); rounds=0 is
    # the malformation, not the length.
    stored = "$pbkdf2-sha512$0$c2FsdA$" + "A" * 86
    assert check_weak_password("someone", stored) == "malformed"


def test_empty_password_hash_ignored():
    assert check_weak_password("someone", "") is None
    assert check_weak_password("someone", "   ") is None


def test_candidates_include_login_and_common():
    cands = weak_password_candidates("bob")
    values = [c for c, _ in cands]
    assert "bob" in values
    for common in _COMMON_WEAK_PASSWORDS:
        assert common in values
    # every single digit and letter is a candidate
    assert "7" in values and "a" in values and "Z" in values


def test_custom_candidates():
    stored = _pbkdf2_hash("letmein")
    assert check_weak_password("bob", stored, candidates=[("letmein", "common")]) == "common"
    # default candidate list does not include it
    assert check_weak_password("bob", stored) is None


def test_truncated_digest_rejected():
    # A digest cut short parses as valid base64 but is the wrong size for the
    # algo — must not silently pass as "not weak".
    assert parse_password_hash(_pbkdf2_hash("x")[:-10] + "==") is None
    assert check_weak_password("someone", "$pbkdf2-sha512$1000$dGVzdHNhbHQ$QUJD") == "malformed"


def test_unparsable_hash_flagged():
    assert check_weak_password("someone", "$argon2$x$y$z") == "malformed"
    assert check_weak_password("someone", "$pbkdf2-sha512$notanumber$s$d") == "malformed"


def test_passlib_positive_control():
    """Cross-check our hand-rolled parser against passlib itself — the library
    Odoo actually uses. Guards the ab64 encoding ('+' as '.') and the sha512
    algo mapping against silent divergence."""
    from passlib.context import CryptContext

    ctx = CryptContext(schemes=["pbkdf2_sha512"], pbkdf2_sha512__rounds=1000)
    stored = ctx.hash("letmein")
    assert check_weak_password("bob", stored, candidates=[("letmein", "common")]) == "common"
    assert check_weak_password("bob", stored, candidates=[("xY9#other", "common")]) is None
