from app.csrf import derive_csrf_token, verify_csrf_token


def test_same_secret_derives_the_same_token() -> None:
    secret = "a-csrf-secret"
    assert derive_csrf_token(secret) == derive_csrf_token(secret)


def test_different_secrets_derive_different_tokens() -> None:
    assert derive_csrf_token("secret-a") != derive_csrf_token("secret-b")


def test_verify_accepts_the_correct_token() -> None:
    secret = "a-csrf-secret"
    assert verify_csrf_token(secret, derive_csrf_token(secret))


def test_verify_rejects_a_wrong_token() -> None:
    assert not verify_csrf_token("a-csrf-secret", "garbage")


def test_verify_rejects_empty_token() -> None:
    assert not verify_csrf_token("a-csrf-secret", "")
