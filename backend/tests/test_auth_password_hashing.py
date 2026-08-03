import base64
import hashlib

from app.security.auth import hash_password, verify_password


def _legacy_hash(password: str, app_secret: str = "legacy-app-secret") -> str:
    salt = hashlib.sha256(app_secret.encode("utf-8")).hexdigest()[:16]
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${salt}${base64.urlsafe_b64encode(digest).decode('utf-8')}"


def test_same_password_produces_different_hashes() -> None:
    assert hash_password("mypassword") != hash_password("mypassword")


def test_correct_password_is_accepted_and_wrong_password_is_rejected() -> None:
    stored_hash = hash_password("correcthorse")

    assert verify_password("correcthorse", stored_hash) is True
    assert verify_password("wrongpassword", stored_hash) is False


def test_legacy_hash_remains_verifiable() -> None:
    stored_hash = _legacy_hash("oldpassword")

    assert verify_password("oldpassword", stored_hash) is True
    assert verify_password("wrongpassword", stored_hash) is False


def test_malformed_or_unknown_hash_is_rejected() -> None:
    assert verify_password("anypassword", "not-a-valid-hash") is False
    assert verify_password("anypassword", "") is False
    assert verify_password("anypassword", "unknown$salt$digest") is False
