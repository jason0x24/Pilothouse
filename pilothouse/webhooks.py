"""Webhook signature verification.

Each upstream uses its own scheme. We implement the real ones — not the
generic HMAC fallback — because in production these are the strings ops
teams actually configure in their webhook UIs:

  * GitHub      `X-Hub-Signature-256: sha256=<hex>` — HMAC-SHA256 of body
                with the secret you set in the repo webhook config.
  * Slack       `X-Slack-Signature: v0=<hex>` plus `X-Slack-Request-Timestamp`
                header; sign string is `v0:<ts>:<body>` with your Signing
                Secret. We also enforce a 5-minute timestamp window to
                blunt replay attacks.
  * PagerDuty   PagerDuty's v3 webhooks sign with `X-PagerDuty-Signature`
                = `v1=<hex>,v1=<hex>` (potentially multiple, to support
                key rotation). Match any.
  * Datadog     Datadog webhooks don't sign by default; we accept a
                shared-secret header `DD-Signature` if configured, else
                fall back to allowing the request (with a logged warning
                when the secret is set but the header is absent).

Each scheme is keyed by a *separate* secret env var so an org can rotate
GitHub independently of Slack etc. Empty secret = verification disabled
for that source (dev-friendly default).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass

from .config import get_settings

log = logging.getLogger(__name__)


@dataclass
class _Settings:
    github: str
    slack: str
    pagerduty: str
    datadog: str
    # Generic fallback — used by /webhooks/generic/{agent_id}.
    generic: str


def _secrets() -> _Settings:
    s = get_settings()
    # Per-source secrets live alongside the connector tokens; we read them
    # from env at call time so rotation doesn't need a restart.
    import os

    return _Settings(
        github=os.getenv("PILOTHOUSE_GITHUB_WEBHOOK_SECRET", ""),
        slack=os.getenv("PILOTHOUSE_SLACK_SIGNING_SECRET", ""),
        pagerduty=os.getenv("PILOTHOUSE_PAGERDUTY_WEBHOOK_SECRET", ""),
        datadog=os.getenv("PILOTHOUSE_DATADOG_WEBHOOK_SECRET", ""),
        generic=s.webhook_secret,
    )


class WebhookVerificationError(Exception):
    pass


def verify(source: str, headers: dict, body: bytes) -> None:
    """Raise WebhookVerificationError if verification fails.

    If the per-source secret is empty, verification is skipped (dev mode);
    no exception is raised. Production deployments set the secret and the
    request is rejected unless the header matches.
    """
    secrets = _secrets()

    if source == "github":
        secret = secrets.github
        if not secret:
            return
        provided = (headers.get("x-hub-signature-256") or "").lower()
        if not provided.startswith("sha256="):
            raise WebhookVerificationError("missing or malformed X-Hub-Signature-256")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(provided, expected):
            raise WebhookVerificationError("X-Hub-Signature-256 mismatch")
        return

    if source == "slack":
        secret = secrets.slack
        if not secret:
            return
        ts = headers.get("x-slack-request-timestamp") or ""
        sig = headers.get("x-slack-signature") or ""
        if not ts or not sig.startswith("v0="):
            raise WebhookVerificationError("missing slack signature headers")
        try:
            ts_int = int(ts)
        except ValueError:
            raise WebhookVerificationError("invalid x-slack-request-timestamp")
        if abs(time.time() - ts_int) > 60 * 5:
            raise WebhookVerificationError("slack request timestamp outside 5-minute window")
        basestring = f"v0:{ts}:".encode() + body
        expected = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig.lower(), expected):
            raise WebhookVerificationError("x-slack-signature mismatch")
        return

    if source == "pagerduty":
        secret = secrets.pagerduty
        if not secret:
            return
        raw = headers.get("x-pagerduty-signature") or ""
        if not raw:
            raise WebhookVerificationError("missing x-pagerduty-signature")
        # Header is comma-separated "v1=<hex>" entries. Accept any match.
        candidates = [c.strip() for c in raw.split(",") if c.strip().startswith("v1=")]
        if not candidates:
            raise WebhookVerificationError("no v1 signatures in x-pagerduty-signature")
        expected = "v1=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        for candidate in candidates:
            if hmac.compare_digest(candidate.lower(), expected):
                return
        raise WebhookVerificationError("x-pagerduty-signature mismatch")

    if source == "datadog":
        secret = secrets.datadog
        if not secret:
            return
        provided = headers.get("dd-signature") or headers.get("dd-webhook-signature") or ""
        if not provided:
            log.warning("datadog webhook secret configured but no DD-Signature header present")
            raise WebhookVerificationError("missing dd-signature")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(provided, expected):
            raise WebhookVerificationError("dd-signature mismatch")
        return

    # alertmanager + generic: use the generic shared secret if set.
    secret = secrets.generic
    if not secret:
        return
    provided = headers.get("x-pilothouse-signature") or ""
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided, expected):
        raise WebhookVerificationError("x-pilothouse-signature mismatch")
