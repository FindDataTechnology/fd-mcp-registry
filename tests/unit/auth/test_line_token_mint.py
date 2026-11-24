"""Unit tests for the per-line 门K line-token mint (capability line-token-mint-at-gateway)."""


import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from auth_server.line_token_mint import (
    LINE_TOKEN_ISSUER,
    LineTokenKeyMissing,
    mint_line_token,
)


@pytest.fixture(scope="module")
def keypair():
    key = ec.generate_private_key(ec.SECP256R1())
    priv_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return priv_pem, pub_pem


@pytest.fixture(autouse=True)
def _key_env(monkeypatch, keypair):
    monkeypatch.setenv("LINE_TOKEN_KEY_WIRE", keypair[0])
    monkeypatch.setenv("LINE_TOKEN_KID_WIRE", "wire-test")


def _decode(token, pub_pem, **kw):
    return pyjwt.decode(
        token,
        pub_pem.encode(),
        algorithms=["ES256"],
        issuer=LINE_TOKEN_ISSUER,
        audience="wire",
        **kw,
    )


def test_mint_verifies_against_line_public_key(keypair):
    pub_pem = keypair[1]
    token = mint_line_token(line="wire", subject="pih8r05mwh8w", email="a@x.com")
    claims = _decode(token, pub_pem)
    assert claims["iss"] == LINE_TOKEN_ISSUER
    assert claims["aud"] == "wire"
    assert claims["source"] == "wire"
    assert claims["sub"] == "pih8r05mwh8w"
    assert claims["email"] == "a@x.com"
    assert claims["exp"] - claims["iat"] <= 600


def test_ttl_clamped_to_600(keypair):
    token = mint_line_token(line="wire", subject="u", ttl_seconds=99999)
    claims = _decode(token, keypair[1])
    assert claims["exp"] - claims["iat"] == 600


def test_kid_in_header(keypair):
    token = mint_line_token(line="wire", subject="u")
    header = pyjwt.get_unverified_header(token)
    assert header["kid"] == "wire-test"
    assert header["alg"] == "ES256"


def test_missing_key_fails_closed(monkeypatch):
    monkeypatch.delenv("LINE_TOKEN_KEY_WIRE", raising=False)
    with pytest.raises(LineTokenKeyMissing):
        mint_line_token(line="wire", subject="u")


def test_empty_subject_rejected():
    with pytest.raises(ValueError):
        mint_line_token(line="wire", subject="   ")


def test_verifying_against_wrong_key_fails(keypair):
    # A token minted for wire must NOT verify against a different key — proves
    # the signature is real (no accidental 'anything verifies').
    other = ec.generate_private_key(ec.SECP256R1())
    other_pub = (
        other.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    token = mint_line_token(line="wire", subject="u")
    with pytest.raises(pyjwt.InvalidSignatureError):
        _decode(token, other_pub)
