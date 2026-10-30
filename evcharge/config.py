"""Settings, read from the environment (compose.yml's env_file)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .tariff import Tariff

PAYPAL_API = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}


@dataclass(frozen=True)
class Settings:
    paypal_env: str = "sandbox"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_webhook_id: str = ""
    currency: str = "GBP"
    tariff: Tariff = Tariff.flat(Decimal("0.45"))
    # Holds the driver can choose from, in display order.
    hold_options: tuple[Decimal, ...] = (Decimal("20.00"), Decimal("50.00"))
    # The one preselected; None means the first.
    hold_default: Decimal | None = None
    connectors: tuple[str, ...] = ("CP1", "CP2")
    # Socket name -> (OCPP charge point id, connector id). Default: each socket is
    # its own charge point, connector 1.
    sockets: dict[str, tuple[str, int]] = field(default_factory=dict)
    # Charge point id -> password (HTTP Basic, OCPP security profile 1/2).
    # Required for any connection through the tunnel; optional on the LAN.
    ocpp_passwords: dict[str, str] = field(default_factory=dict)
    ocpp_port: int = 9000
    # Charge points allowed to use plain ws:// (directly, from a private address) because
    # they can't do wss://. Everything else must come in over wss:// through the tunnel.
    ocpp_allow_ws: frozenset[str] = frozenset()
    # Stop the charger when the running cost is this close to the hold, leaving
    # room for the energy delivered before the stop takes effect.
    stop_margin: Decimal = Decimal("0.50")
    # The same for sessions limited by energy (admin-authorised by kWh).
    stop_margin_wh: int = 300
    # Admin pages (/admin, church LAN only). Unset: no admin pages at all.
    admin_password: str = ""
    # Read-only JSON for Home Assistant (/api/status, LAN only), as a Bearer token.
    api_token: str = ""
    db_path: str = "ev.sqlite3"
    # An authorisation nobody starts charging on is voided after this long.
    unused_auth_minutes: int = 15
    # Per socket, shown beside its name: kW, and the sockets it shares that power with.
    socket_kw: dict[str, int] = field(default_factory=dict)
    socket_shares: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Branding (all optional): the header name, a logo file in brand_dir, the
    # accent colour, the line under every page, and a note below that.
    brand_name: str = ""
    brand_logo: str = ""
    brand_dir: str = "/brand"
    brand_color: str = "#1f5f7a"
    footer_text: str = ""
    # Shown when a payment goes wrong, e.g. "Please call the church office on 01234 567890."
    support_contact: str = ""
    # The site's public address, e.g. https://ev.example.org. When set, the PayPal
    # order description carries the driver's link back to their session.
    public_url: str = ""
    # PayPal invoice ids are this + the session id; PayPal wants them unique per account.
    invoice_prefix: str = "EV-"
    page_note: str = ""
    # "card": only PayPal's guest card checkout (1.2% + 30p); "all": PayPal accounts too (2.9% + 30p).
    payment_methods: str = "card"

    def __post_init__(self):
        if self.hold_default is None:
            object.__setattr__(self, "hold_default", self.hold_options[0])
        elif self.hold_default not in self.hold_options:
            raise ValueError("HOLD_DEFAULT must be one of HOLD_OPTIONS")
        if not self.sockets:
            object.__setattr__(self, "sockets", {c: (c, 1) for c in self.connectors})

    def power_label(self, socket: str) -> str:
        kw = self.socket_kw.get(socket)
        if not kw:
            return ""
        shares = self.socket_shares.get(socket)
        return f"{kw} kW" + (f" (shared with {', '.join(shares)})" if shares else "")

    def socket_for(self, charge_point: str, connector: int) -> str | None:
        return next((s for s, cp in self.sockets.items() if cp == (charge_point, connector)), None)

    @property
    def charge_points(self) -> set[str]:
        return {cp for cp, _ in self.sockets.values()}

    @property
    def paypal_api(self) -> str:
        return PAYPAL_API[self.paypal_env]

    @property
    def paypal_configured(self) -> bool:
        return bool(self.paypal_client_id and self.paypal_client_secret)

    def kwh_for(self, hold: Decimal, price: Decimal) -> int:
        """Whole kWh a hold covers if every kWh is at `price`, after the session fee.
        The charger itself is stopped when the running cost reaches the hold."""
        return int((hold - self.tariff.session_fee) // price)


def _money(name: str, value: str) -> Decimal:
    try:
        d = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{name}={value!r} is not a number") from None
    if d <= 0:
        raise ValueError(f"{name} must be positive")
    return d


def _tariff(env) -> Tariff:
    solar = env.get("SOLAR_PRICE_PER_KWH")
    solar_price = Decimal(solar) if solar else None
    fee = Decimal(env.get("SESSION_FEE") or "0")
    if fee < 0:
        raise ValueError("SESSION_FEE can't be negative")
    if env.get("TARIFF"):
        return Tariff.parse(env["TARIFF"], solar_price, fee)
    return Tariff.flat(_money("PRICE_PER_KWH", env.get("PRICE_PER_KWH", "0.45")), solar_price, fee)


def _sockets(value: str) -> dict[str, tuple[str, int]]:
    """'CP1=CP1:1, CP2=CP2:1' -> {'CP1': ('CP1', 1), ...}"""
    out = {}
    for part in filter(None, (p.strip() for p in value.split(","))):
        name, _, target = part.partition("=")
        cp, _, conn = target.partition(":")
        if not (name and cp):
            raise ValueError(f"SOCKETS entry {part!r} should look like 'CP1=CP1:1'")
        out[name.strip()] = (cp.strip(), int(conn or 1))
    return out


def _holds(value: str) -> tuple[Decimal, ...]:
    holds = tuple(_money("HOLD_OPTIONS", v.strip()).quantize(Decimal("0.01")) for v in value.split(",") if v.strip())
    if not holds:
        raise ValueError("HOLD_OPTIONS is empty")
    return holds


def from_env(env: dict[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    d = Settings()
    s = Settings(
        paypal_env=env.get("PAYPAL_ENV", d.paypal_env),
        paypal_client_id=env.get("PAYPAL_CLIENT_ID", ""),
        paypal_client_secret=env.get("PAYPAL_CLIENT_SECRET", ""),
        paypal_webhook_id=env.get("PAYPAL_WEBHOOK_ID", ""),
        currency=env.get("CURRENCY", d.currency),
        tariff=_tariff(env),
        hold_options=_holds(env.get("HOLD_OPTIONS") or env.get("HOLD_AMOUNT") or ",".join(map(str, d.hold_options))),
        connectors=tuple(c.strip() for c in env.get("CONNECTORS", ",".join(d.connectors)).split(",") if c.strip()),
        db_path=env.get("DB_PATH", d.db_path),
        unused_auth_minutes=int(env.get("UNUSED_AUTH_MINUTES", d.unused_auth_minutes)),
        brand_name=env.get("BRAND_NAME", ""),
        brand_logo=env.get("BRAND_LOGO", ""),
        brand_dir=env.get("BRAND_DIR", d.brand_dir),
        brand_color=env.get("BRAND_COLOR") or d.brand_color,
        footer_text=env.get("FOOTER_TEXT", ""),
        support_contact=env.get("SUPPORT_CONTACT", ""),
        invoice_prefix=env.get("INVOICE_PREFIX") or d.invoice_prefix,
        public_url=env.get("PUBLIC_URL", "").rstrip("/"),
        payment_methods=env.get("PAYMENT_METHODS", d.payment_methods),
        page_note=env.get("PAGE_NOTE", ""),
        socket_kw={k.removeprefix("SOCKET_KW_"): int(v) for k, v in env.items() if k.startswith("SOCKET_KW_") and v},
        socket_shares={k.removeprefix("SOCKET_SHARES_"): tuple(x.strip() for x in v.split(",") if x.strip())
                       for k, v in env.items() if k.startswith("SOCKET_SHARES_") and v},
        hold_default=_money("HOLD_DEFAULT", env["HOLD_DEFAULT"]) if env.get("HOLD_DEFAULT") else None,
        sockets=_sockets(env.get("SOCKETS", "")),
        ocpp_passwords={k.removeprefix("OCPP_PASSWORD_"): v for k, v in env.items()
                        if k.startswith("OCPP_PASSWORD_") and v},
        ocpp_port=int(env.get("OCPP_PORT", d.ocpp_port)),
        ocpp_allow_ws=frozenset(k.removeprefix("OCPP_ALLOW_WS_") for k, v in env.items()
                                if k.startswith("OCPP_ALLOW_WS_") and v.lower() in ("1", "true", "yes")),
        stop_margin=Decimal(env.get("STOP_MARGIN", str(d.stop_margin))),
        stop_margin_wh=int(env.get("STOP_MARGIN_WH", d.stop_margin_wh)),
        admin_password=env.get("ADMIN_PASSWORD", ""),
        api_token=env.get("API_TOKEN", ""),
    )
    unknown = set(s.sockets) - set(s.connectors)
    if unknown:
        raise ValueError(f"SOCKETS names {sorted(unknown)} not in CONNECTORS")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", s.brand_color):
        raise ValueError("BRAND_COLOR must look like #7b0303")
    if s.brand_logo and ("/" in s.brand_logo or s.brand_logo.startswith(".")):
        raise ValueError("BRAND_LOGO is a file name inside BRAND_DIR, not a path")
    for name in (*s.socket_kw, *s.socket_shares):
        if name not in s.connectors:
            raise ValueError(f"SOCKET_KW_/SOCKET_SHARES_ name {name} not in CONNECTORS")
    if s.public_url and not s.public_url.startswith("https://"):
        raise ValueError("PUBLIC_URL must start with https://")
    if s.payment_methods not in ("card", "all"):
        raise ValueError("PAYMENT_METHODS must be card or all")
    if s.paypal_env not in PAYPAL_API:
        raise ValueError(f"PAYPAL_ENV must be one of {sorted(PAYPAL_API)}")
    return s
