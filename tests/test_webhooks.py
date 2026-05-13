"""Per-source webhook signature verification."""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from pilothouse.webhooks import WebhookVerificationError, verify


def _sig(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_no_secret_means_no_verification(monkeypatch) -> None:
    # No secret in env → all sources accept anything (dev default).
    for source in ("github", "slack", "pagerduty", "datadog", "generic", "alertmanager"):
        verify(source, {}, b"hi")  # should not raise


def test_github_signature_ok(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_GITHUB_WEBHOOK_SECRET", "shh")
    body = b'{"hello":"world"}'
    sig = "sha256=" + _sig("shh", body)
    verify("github", {"x-hub-signature-256": sig}, body)


def test_github_signature_mismatch(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_GITHUB_WEBHOOK_SECRET", "shh")
    with pytest.raises(WebhookVerificationError):
        verify("github", {"x-hub-signature-256": "sha256=wrong"}, b"{}")


def test_github_missing_header(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_GITHUB_WEBHOOK_SECRET", "shh")
    with pytest.raises(WebhookVerificationError):
        verify("github", {}, b"{}")


def test_slack_signature_ok(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_SLACK_SIGNING_SECRET", "sek")
    body = b"payload=foo"
    ts = str(int(time.time()))
    basestring = f"v0:{ts}:".encode() + body
    sig = "v0=" + _sig("sek", basestring)
    verify(
        "slack",
        {"x-slack-request-timestamp": ts, "x-slack-signature": sig},
        body,
    )


def test_slack_old_timestamp_rejected(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_SLACK_SIGNING_SECRET", "sek")
    body = b"x"
    ts = str(int(time.time()) - 3600)  # 1h ago > 5min window
    basestring = f"v0:{ts}:".encode() + body
    sig = "v0=" + _sig("sek", basestring)
    with pytest.raises(WebhookVerificationError):
        verify(
            "slack",
            {"x-slack-request-timestamp": ts, "x-slack-signature": sig},
            body,
        )


def test_pagerduty_multi_signature(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_PAGERDUTY_WEBHOOK_SECRET", "pd")
    body = b'{"event":"x"}'
    good = "v1=" + _sig("pd", body)
    header = f"v1=deadbeef,{good}"
    verify("pagerduty", {"x-pagerduty-signature": header}, body)


def test_datadog_secret_present_header_missing(monkeypatch) -> None:
    monkeypatch.setenv("PILOTHOUSE_DATADOG_WEBHOOK_SECRET", "dd")
    with pytest.raises(WebhookVerificationError):
        verify("datadog", {}, b"x")
