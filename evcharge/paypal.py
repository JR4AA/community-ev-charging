"""Thin client for the PayPal REST calls we use.

Orders v2 with intent AUTHORIZE: create order -> buyer approves in the PayPal
buttons -> authorize order (places the hold) -> capture the actual cost with
final_capture, which releases the rest of the hold. Or void, if nothing was used.

Every POST carries a PayPal-Request-Id derived from our session, so a retry
after a timeout returns the original result instead of doing it twice.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import httpx

from .config import Settings


class PayPalError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        name = body.get("name") if isinstance(body, dict) else None
        super().__init__(f"PayPal {status} {name or ''}".strip())


def _amount(currency: str, value: Decimal) -> dict[str, str]:
    return {"currency_code": currency, "value": f"{value:.2f}"}


class PayPal:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._http = httpx.Client(base_url=settings.paypal_api, timeout=30, transport=transport)
        self._token: str | None = None
        self._token_expires = 0.0

    def _access_token(self, refresh: bool = False) -> str:
        if self._token and not refresh and time.monotonic() < self._token_expires:
            return self._token
        r = self._http.post(
            "/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(self.settings.paypal_client_id, self.settings.paypal_client_secret),
        )
        if r.status_code != 200:
            raise PayPalError(r.status_code, _body(r))
        body = r.json()
        self._token = body["access_token"]
        self._token_expires = time.monotonic() + int(body.get("expires_in", 300)) - 60
        return self._token

    def _call(self, method: str, path: str, json: Any = None, request_id: str | None = None) -> dict:
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self._access_token(refresh=attempt > 0)}"}
            if request_id:
                headers["PayPal-Request-Id"] = request_id
            r = self._http.request(method, path, json=json, headers=headers)
            if r.status_code == 401 and attempt == 0:
                continue  # token revoked or expired early
            if r.status_code >= 400:
                raise PayPalError(r.status_code, _body(r))
            return r.json() if r.content else {}
        raise AssertionError("unreachable")

    def create_order(self, *, amount: Decimal, reference: str, description: str, request_id: str) -> dict:
        return self._call("POST", "/v2/checkout/orders", request_id=request_id, json={
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "reference_id": reference,
                "custom_id": reference,
                "description": description,
                "amount": _amount(self.settings.currency, amount),
            }],
            "application_context": {
                **({"brand_name": self.settings.brand_name[:127]} if self.settings.brand_name else {}),
                "shipping_preference": "NO_SHIPPING",
                "user_action": "CONTINUE",
            },
        })

    def authorize_order(self, order_id: str, *, request_id: str) -> dict:
        """Returns the authorization object (id, status, expiration_time, amount)."""
        order = self._call("POST", f"/v2/checkout/orders/{order_id}/authorize", json={}, request_id=request_id)
        return order["purchase_units"][0]["payments"]["authorizations"][0]

    def capture_authorization(self, auth_id: str, *, amount: Decimal, invoice_id: str, request_id: str) -> dict:
        return self._call("POST", f"/v2/payments/authorizations/{auth_id}/capture", request_id=request_id, json={
            "amount": _amount(self.settings.currency, amount),
            "invoice_id": invoice_id,
            "final_capture": True,
        })

    def void_authorization(self, auth_id: str, *, request_id: str) -> None:
        self._call("POST", f"/v2/payments/authorizations/{auth_id}/void", request_id=request_id)

    def verify_webhook(self, headers: dict[str, str], event: dict) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        try:
            body = {
                "auth_algo": h["paypal-auth-algo"],
                "cert_url": h["paypal-cert-url"],
                "transmission_id": h["paypal-transmission-id"],
                "transmission_sig": h["paypal-transmission-sig"],
                "transmission_time": h["paypal-transmission-time"],
                "webhook_id": self.settings.paypal_webhook_id,
                "webhook_event": event,
            }
        except KeyError:
            return False
        result = self._call("POST", "/v1/notifications/verify-webhook-signature", json=body)
        return result.get("verification_status") == "SUCCESS"


def _body(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text[:500]
