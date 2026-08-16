"""Security primitive tests: keys, passwords, tokens, cursors, permissions."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from evalforge_api.errors import UnauthorizedError
from evalforge_api.security import cursors, keys, passwords, tokens
from evalforge_api.security.permissions import (
    Permission,
    Principal,
    permissions_for_role,
    permissions_for_scopes,
)

SECRET = "test-secret-value-that-is-long-enough-32"


class TestApiKeys:
    def test_generated_key_has_the_documented_shape(self) -> None:
        generated = keys.generate("prod")
        parts = generated.token.split("_", 3)
        assert parts[0] == "ef"
        assert parts[1] == "prod"
        assert len(parts) == 4
        assert generated.token.startswith(generated.prefix)

    def test_every_generated_key_parses(self) -> None:
        """base64url includes `_`, so about half of all secrets contain one.

        An unbounded split treated those as malformed, which would have rejected
        roughly half of every project's keys at authentication time — intermittent,
        unreproducible, and impossible to diagnose from the outside.
        """
        for _ in range(500):
            generated = keys.generate("prod")
            assert keys.parse_prefix(generated.token) == generated.prefix

    def test_a_secret_containing_underscores_still_verifies(self) -> None:
        token = "ef_prod_abcd1234_secret_with_many_underscores_in_it"
        assert keys.parse_prefix(token) == "ef_prod_abcd1234"
        assert keys.verify(token, keys.hash_key(token))

    def test_the_prefix_is_detectable_by_a_secret_scanner(self) -> None:
        """The `ef_` prefix is what lets gitleaks and GitHub flag a leaked key."""
        assert keys.generate().token.startswith("ef_")

    def test_only_a_digest_is_retained(self) -> None:
        generated = keys.generate()
        assert len(generated.key_hash) == 32
        # The secret must not be recoverable from what we store.
        assert generated.token.encode() not in generated.key_hash

    def test_verification_round_trips(self) -> None:
        generated = keys.generate()
        assert keys.verify(generated.token, generated.key_hash)

    def test_a_wrong_key_does_not_verify(self) -> None:
        generated = keys.generate()
        assert not keys.verify(keys.generate().token, generated.key_hash)

    def test_keys_are_unique(self) -> None:
        assert len({keys.generate().token for _ in range(200)}) == 200

    def test_secret_entropy_is_at_least_256_bits(self) -> None:
        secret = keys.generate().token.split("_", 3)[3]
        assert len(secret) >= 43  # base64url of 32 bytes

    @pytest.mark.parametrize(
        "token",
        ["", "garbage", "ef_prod", "ef_prod_abc", "xx_prod_abc_def", "ef__abc_def", "ef_prod__def"],
    )
    def test_malformed_tokens_are_rejected_before_any_query(self, token: str) -> None:
        assert keys.parse_prefix(token) is None

    def test_the_environment_is_visible_in_the_key(self) -> None:
        """A staging key should be obviously not a production key.

        Short forms rather than the full word: truncating to eight characters produced
        `ef_producti_…`, which looks like a typo on the one string a user copies, pastes, and shows
        to a colleague. The property that matters is that the two are unmistakable at a glance.
        """
        assert keys.generate("staging").token.startswith("ef_stg_")
        assert keys.generate("production").token.startswith("ef_prod_")
        assert keys.generate("development").token.startswith("ef_dev_")

    def test_environment_is_sanitised(self) -> None:
        assert keys.generate("../../etc").prefix.split("_")[1].isalnum()


class TestPasswords:
    def test_hash_and_verify(self) -> None:
        stored = passwords.hash_password("correct horse battery staple")
        assert passwords.verify_password("correct horse battery staple", stored)

    def test_a_wrong_password_fails(self) -> None:
        stored = passwords.hash_password("correct horse battery staple")
        assert not passwords.verify_password("wrong", stored)

    def test_the_hash_is_salted(self) -> None:
        a = passwords.hash_password("same password here")
        b = passwords.hash_password("same password here")
        assert a != b

    def test_the_plaintext_is_not_in_the_hash(self) -> None:
        assert "battery" not in passwords.hash_password("correct horse battery staple")

    def test_short_passwords_are_rejected(self) -> None:
        with pytest.raises(passwords.WeakPasswordError):
            passwords.hash_password("short")

    def test_absurdly_long_passwords_are_rejected(self) -> None:
        """Unbounded input to a memory-hard function is a denial-of-service vector."""
        with pytest.raises(passwords.WeakPasswordError):
            passwords.hash_password("x" * 5000)

    def test_a_corrupt_stored_hash_fails_closed(self) -> None:
        assert not passwords.verify_password("anything", "not-a-hash")

    def test_hashing_is_deliberately_slow(self) -> None:
        """The cost parameter is the defence; a fast hash would mean none."""
        started = time.perf_counter()
        passwords.hash_password("correct horse battery staple")
        assert (time.perf_counter() - started) > 0.005


class TestTokens:
    def test_access_token_round_trips(self) -> None:
        user_id = uuid.uuid4()
        token, expires = tokens.create_access_token(user_id, secret=SECRET)
        claims = tokens.decode_access_token(token, secret=SECRET)
        assert claims.subject == str(user_id)
        assert expires > datetime.now(UTC)

    def test_a_token_signed_with_another_secret_is_rejected(self) -> None:
        token, _ = tokens.create_access_token(
            uuid.uuid4(), secret="another-secret-of-at-least-32-bytes-long"
        )
        with pytest.raises(UnauthorizedError):
            tokens.decode_access_token(token, secret=SECRET)

    def test_an_expired_token_is_rejected(self) -> None:
        token, _ = tokens.create_access_token(uuid.uuid4(), secret=SECRET, ttl_s=-10)
        with pytest.raises(UnauthorizedError, match="expired"):
            tokens.decode_access_token(token, secret=SECRET)

    def test_the_none_algorithm_is_refused(self) -> None:
        """The classic JWT bypass: never trust the token's own `alg` header."""
        forged = jwt.encode({"sub": "x", "exp": 9999999999, "iat": 0}, key="", algorithm="none")
        with pytest.raises(UnauthorizedError):
            tokens.decode_access_token(forged, secret=SECRET)

    def test_a_refresh_token_is_not_accepted_as_an_access_token(self) -> None:
        """Otherwise a 30-day credential silently gains a 15-minute one's authority."""
        payload = {
            "sub": str(uuid.uuid4()),
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(days=30)).timestamp()),
            "typ": "refresh",
        }
        forged = jwt.encode(payload, SECRET, algorithm="HS256")
        with pytest.raises(UnauthorizedError, match="token type"):
            tokens.decode_access_token(forged, secret=SECRET)

    def test_refresh_tokens_are_opaque_and_hashed(self) -> None:
        token, digest = tokens.generate_refresh_token()
        assert len(digest) == 32
        assert tokens.hash_refresh_token(token) == digest
        assert "." not in token  # not a JWT; it must be revocable server-side


class TestCursors:
    def test_round_trip(self) -> None:
        payload = {"started_at": "2026-01-01T00:00:00Z", "id": "abc"}
        assert cursors.decode(cursors.encode(payload, secret=SECRET), secret=SECRET) == payload

    def test_a_tampered_cursor_is_rejected(self) -> None:
        """An unsigned cursor is a client-controlled value that reaches the WHERE
        clause — which is how pagination becomes a cross-tenant read."""
        cursor = cursors.encode({"id": "mine"}, secret=SECRET)
        body, signature = cursor.split(".")
        forged = cursors.encode({"id": "someone-elses"}, secret=SECRET).split(".")[0]
        with pytest.raises(cursors.InvalidCursorError):
            cursors.decode(f"{forged}.{signature}", secret=SECRET)
        assert body != forged

    def test_a_cursor_from_another_deployment_is_rejected(self) -> None:
        cursor = cursors.encode({"id": "x"}, secret="a-different-secret-of-32-plus-bytes-x")
        with pytest.raises(cursors.InvalidCursorError):
            cursors.decode(cursor, secret=SECRET)

    @pytest.mark.parametrize("cursor", ["", "garbage", "a.b", "....", "abc."])
    def test_malformed_cursors_are_rejected(self, cursor: str) -> None:
        with pytest.raises(cursors.InvalidCursorError):
            cursors.decode(cursor, secret=SECRET)

    def test_the_error_does_not_reveal_why_it_failed(self) -> None:
        """'Bad signature' tells a prober they are close."""
        cursor = cursors.encode({"id": "x"}, secret="other-secret-value-at-least-32-bytes")
        with pytest.raises(cursors.InvalidCursorError) as exc:
            cursors.decode(cursor, secret=SECRET)
        assert "signature" not in str(exc.value).lower()

    def test_datetimes_survive_encoding(self) -> None:
        payload = {"ts": datetime(2026, 3, 1, tzinfo=UTC)}
        assert "2026-03-01" in str(
            cursors.decode(cursors.encode(payload, secret=SECRET), secret=SECRET)
        )


class TestPermissionMatrix:
    def test_roles_are_strictly_nested(self) -> None:
        viewer = permissions_for_role("viewer")
        reviewer = permissions_for_role("reviewer")
        developer = permissions_for_role("developer")
        admin = permissions_for_role("admin")
        assert viewer < reviewer < developer < admin

    def test_a_viewer_cannot_write(self) -> None:
        viewer = permissions_for_role("viewer")
        assert Permission.PROJECT_READ in viewer
        assert Permission.DATASET_WRITE not in viewer
        assert Permission.KEYS_MANAGE not in viewer

    def test_a_developer_cannot_manage_keys_or_members(self) -> None:
        developer = permissions_for_role("developer")
        assert Permission.KEYS_MANAGE not in developer
        assert Permission.MEMBERS_MANAGE not in developer

    def test_a_reviewer_can_annotate_but_not_lock_datasets(self) -> None:
        """Locking a golden dataset is a curation act reserved for developers."""
        reviewer = permissions_for_role("reviewer")
        assert Permission.ANNOTATION_WRITE in reviewer
        assert Permission.DATASET_LOCK not in reviewer

    def test_an_unknown_role_grants_nothing(self) -> None:
        assert permissions_for_role("superuser") == frozenset()

    def test_an_ingest_key_cannot_read_traces_back(self) -> None:
        """A key leaked from a container image should not become a data export."""
        ingest = permissions_for_scopes(["ingest"])
        assert Permission.TRACE_INGEST in ingest
        assert Permission.PROJECT_READ not in ingest

    def test_an_unknown_scope_grants_nothing(self) -> None:
        assert permissions_for_scopes(["admin", "root"]) == frozenset()

    def test_principal_permission_check(self) -> None:
        principal = Principal(
            kind="user", id="u", permissions=permissions_for_role("developer"), role="developer"
        )
        assert principal.can(Permission.DATASET_WRITE)
        assert not principal.can(Permission.KEYS_MANAGE)
