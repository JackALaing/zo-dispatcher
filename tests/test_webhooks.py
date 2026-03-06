"""Tests for webhook processing: signatures, event matching, transforms, deduplication."""

import base64
import hashlib
import hmac as hmac_mod
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zo_dispatcher.webhooks import (
    verify_signature,
    apply_transform,
    event_matches,
    _get_nested_value,
    load_transform_module,
    _transform_cache,
)


# --- Signature verification ---

class TestSignatureVerification:
    def test_hmac_sha256_hex_valid(self):
        secret = "test_secret_123"
        body = b'{"event": "test"}'
        sig = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()

        config = {
            "source": "test",
            "secret_env": "TEST_SECRET",
            "signature_algo": "hmac-sha256-hex",
            "signature_prefix": "",
        }
        with patch.dict(os.environ, {"TEST_SECRET": secret}):
            assert verify_signature(config, sig, body) is True

    def test_hmac_sha256_hex_invalid(self):
        config = {
            "source": "test",
            "secret_env": "TEST_SECRET",
            "signature_algo": "hmac-sha256-hex",
            "signature_prefix": "",
        }
        with patch.dict(os.environ, {"TEST_SECRET": "secret"}):
            assert verify_signature(config, "bad_sig", b"body") is False

    def test_hmac_sha256_hex_missing_header(self):
        config = {
            "source": "test",
            "secret_env": "TEST_SECRET",
            "signature_algo": "hmac-sha256-hex",
            "signature_prefix": "",
        }
        with patch.dict(os.environ, {"TEST_SECRET": "secret"}):
            assert verify_signature(config, None, b"body") is False

    def test_prefix_stripping(self):
        secret = "github_secret"
        body = b'{"action": "push"}'
        sig = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()

        config = {
            "source": "github",
            "secret_env": "GH_SECRET",
            "signature_algo": "hmac-sha256-hex",
            "signature_prefix": "sha256=",
        }
        with patch.dict(os.environ, {"GH_SECRET": secret}):
            assert verify_signature(config, f"sha256={sig}", body) is True
            assert verify_signature(config, "sha256=wrong", body) is False

    def test_hmac_sha1_hex(self):
        secret = "sha1_secret"
        body = b"test body"
        sig = hmac_mod.new(secret.encode(), body, hashlib.sha1).hexdigest()

        config = {
            "source": "legacy",
            "secret_env": "LEGACY_SECRET",
            "signature_algo": "hmac-sha1-hex",
            "signature_prefix": "",
        }
        with patch.dict(os.environ, {"LEGACY_SECRET": secret}):
            assert verify_signature(config, sig, body) is True
            assert verify_signature(config, "wrong", body) is False

    def test_hmac_sha256_base64(self):
        secret = "b64_secret"
        body = b'{"data": true}'
        sig = base64.b64encode(
            hmac_mod.new(secret.encode(), body, hashlib.sha256).digest()
        ).decode()

        config = {
            "source": "b64test",
            "secret_env": "B64_SECRET",
            "signature_algo": "hmac-sha256-base64",
            "signature_prefix": "",
        }
        with patch.dict(os.environ, {"B64_SECRET": secret}):
            assert verify_signature(config, sig, body) is True
            assert verify_signature(config, "bad", body) is False

    def test_unsigned_webhook_rejected_by_default(self):
        config = {"source": "unsigned", "secret_env": None, "signature_algo": None}
        assert verify_signature(config, None, b"anything") is False

    def test_unsigned_webhook_accepted_with_allow_unsigned(self):
        config = {"source": "unsigned", "secret_env": None, "signature_algo": None, "allow_unsigned": True}
        assert verify_signature(config, None, b"anything") is True

    def test_custom_verification(self):
        mock_mod = MagicMock()
        mock_mod.verify.return_value = True

        config = {
            "source": "custom_src",
            "secret_env": "CUSTOM_SECRET",
            "signature_algo": "custom",
            "transform_script": "custom.py",
        }
        with patch("zo_dispatcher.webhooks.load_transform_module", return_value=mock_mod):
            with patch.dict(os.environ, {"CUSTOM_SECRET": "sec"}):
                assert verify_signature(config, "sig_val", b"body") is True
                mock_mod.verify.assert_called_once_with("sig_val", b"body", "sec")

        mock_mod.verify.return_value = False
        with patch("zo_dispatcher.webhooks.load_transform_module", return_value=mock_mod):
            with patch.dict(os.environ, {"CUSTOM_SECRET": "sec"}):
                assert verify_signature(config, "sig_val", b"body") is False


# --- Event matching ---

class TestEventMatching:
    def test_source_wildcard(self):
        assert event_matches("stripe", "stripe", "checkout.session.completed") is True
        assert event_matches("stripe", "stripe", None) is True

    def test_full_event_match(self):
        assert event_matches(
            "stripe.checkout.session.completed", "stripe", "checkout.session.completed"
        ) is True

    def test_prefix_match(self):
        assert event_matches(
            "stripe.checkout", "stripe", "checkout.session.completed"
        ) is True

    def test_different_source(self):
        assert event_matches("github.push", "stripe", "push") is False

    def test_wrong_event(self):
        assert event_matches(
            "stripe.payment_intent.succeeded", "stripe", "checkout.session.completed"
        ) is False

    def test_more_specific_no_match(self):
        assert event_matches(
            "stripe.checkout.session.completed.extra",
            "stripe",
            "checkout.session.completed",
        ) is False


class TestMultiEventMatching:
    def test_list_matches_any(self):
        events = ["github.push", "github.pull_request", "linear.issue"]
        assert event_matches(events, "github", "push") is True
        assert event_matches(events, "linear", "issue") is True

    def test_list_no_match(self):
        events = ["github.push", "linear.issue"]
        assert event_matches(events, "stripe", "checkout") is False

    def test_list_prefix_match(self):
        events = ["github.pull_request"]
        assert event_matches(events, "github", "pull_request.opened") is True
        assert event_matches(events, "github", "pull_request.merged") is True

    def test_list_source_wildcard(self):
        events = ["github", "linear.issue"]
        assert event_matches(events, "github", "push") is True
        assert event_matches(events, "github", "pull_request.opened") is True

    def test_list_cross_source(self):
        events = ["github.push", "linear.issue.created", "todoist.item"]
        assert event_matches(events, "github", "push") is True
        assert event_matches(events, "linear", "issue.created") is True
        assert event_matches(events, "todoist", "item.added") is True
        assert event_matches(events, "stripe", "checkout") is False

    def test_single_element_list(self):
        assert event_matches(["stripe.checkout"], "stripe", "checkout.session.completed") is True

    def test_empty_list_no_match(self):
        assert event_matches([], "stripe", "checkout") is False

    def test_backward_compat_string(self):
        assert event_matches("stripe.checkout", "stripe", "checkout.session.completed") is True


# --- JSON path extraction ---

class TestGetNestedValue:
    def test_simple_path(self):
        data = {"type": "checkout.session.completed"}
        assert _get_nested_value(data, "type") == "checkout.session.completed"

    def test_nested_path(self):
        data = {"data": {"object": {"amount": 100}}}
        assert _get_nested_value(data, "data.object.amount") == "100"

    def test_dollar_prefix(self):
        data = {"type": "test"}
        assert _get_nested_value(data, "$.type") == "test"

    def test_missing_key(self):
        data = {"type": "test"}
        assert _get_nested_value(data, "nonexistent") is None

    def test_empty_path(self):
        data = {"type": "test"}
        assert _get_nested_value(data, "") is None

    def test_partial_path(self):
        data = {"a": {"b": 1}}
        assert _get_nested_value(data, "a.b.c") is None


# --- Transform loading and application ---

class TestTransforms:
    def test_load_and_apply(self, tmp_path):
        _transform_cache.clear()
        transforms_dir = tmp_path / "transforms"
        transforms_dir.mkdir()

        script = transforms_dir / "test_transform.py"
        script.write_text("""\
def transform(payload, event_type):
    return {"transformed": True, "event_was": event_type}

def verify(signature, body, secret):
    return signature == "valid"
""")

        config = {"source": "test", "transform_script": "test_transform.py"}
        mod = load_transform_module(config, transforms_dir)
        assert mod is not None
        assert hasattr(mod, "transform")
        assert hasattr(mod, "verify")

        result = apply_transform(config, {"data": "original"}, "test.event", transforms_dir)
        assert result["transformed"] is True
        assert result["event_was"] == "test.event"

        _transform_cache.clear()

    def test_no_transform_script(self):
        result = apply_transform({"source": "noscript"}, {"data": "unchanged"}, "x")
        assert result == {"data": "unchanged"}

    def test_path_traversal_blocked(self, tmp_path):
        _transform_cache.clear()
        transforms_dir = tmp_path / "transforms"
        transforms_dir.mkdir()

        config = {"source": "bad", "transform_script": "../../../etc/passwd"}
        mod = load_transform_module(config, transforms_dir)
        assert mod is None

        _transform_cache.clear()

    def test_mtime_based_caching(self, tmp_path):
        _transform_cache.clear()
        transforms_dir = tmp_path / "transforms"
        transforms_dir.mkdir()

        script = transforms_dir / "cached.py"
        script.write_text("def transform(p, e): return {'v': 1}\n")

        config = {"source": "test", "transform_script": "cached.py"}
        mod1 = load_transform_module(config, transforms_dir)
        mod2 = load_transform_module(config, transforms_dir)
        assert mod1 is mod2  # same cached object

        _transform_cache.clear()

    def test_transform_exception_returns_original(self, tmp_path):
        _transform_cache.clear()
        transforms_dir = tmp_path / "transforms"
        transforms_dir.mkdir()

        script = transforms_dir / "bad_transform.py"
        script.write_text("def transform(p, e): raise ValueError('boom')\n")

        config = {"source": "test", "transform_script": "bad_transform.py"}
        original = {"data": "keep"}
        result = apply_transform(config, original, "x", transforms_dir)
        assert result == original

        _transform_cache.clear()

    def test_transform_returns_none_drops_event(self, tmp_path):
        _transform_cache.clear()
        transforms_dir = tmp_path / "transforms"
        transforms_dir.mkdir()

        script = transforms_dir / "drop_transform.py"
        script.write_text("def transform(p, e): return None\n")

        config = {"source": "test", "transform_script": "drop_transform.py"}
        result = apply_transform(config, {"data": "original"}, "test.event", transforms_dir)
        assert result is None

        _transform_cache.clear()

    def test_transform_returns_dict_passes_through(self, tmp_path):
        _transform_cache.clear()
        transforms_dir = tmp_path / "transforms"
        transforms_dir.mkdir()

        script = transforms_dir / "dict_transform.py"
        script.write_text("def transform(p, e): return {'kept': True}\n")

        config = {"source": "test", "transform_script": "dict_transform.py"}
        result = apply_transform(config, {"data": "original"}, "test.event", transforms_dir)
        assert result == {"kept": True}

        _transform_cache.clear()

    def test_transform_returns_empty_dict_not_dropped(self, tmp_path):
        _transform_cache.clear()
        transforms_dir = tmp_path / "transforms"
        transforms_dir.mkdir()

        script = transforms_dir / "empty_transform.py"
        script.write_text("def transform(p, e): return {}\n")

        config = {"source": "test", "transform_script": "empty_transform.py"}
        result = apply_transform(config, {"data": "original"}, "test.event", transforms_dir)
        assert result == {}
        assert result is not None

        _transform_cache.clear()
