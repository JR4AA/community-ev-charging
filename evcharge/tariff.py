"""Time-of-use pricing, plus an optional solar-surplus rate.

Bands are local (Europe/London) wall-clock times covering the whole day, e.g.

    TARIFF="night 00:00-07:00 0.35; day 07:00-16:00 0.45; peak 16:00-19:00 0.65; day 19:00-24:00 0.45"

Energy is priced by when it was delivered: each pair of meter readings is split
at band boundaries (and solar on/off times), and the energy shared out in
proportion to time. While the solar flag is on, the solar rate replaces the band.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

TZ = "Europe/London"
SOLAR = "solar"
FEE = "fee"
_BAND = re.compile(r"^(?P<name>[a-z][a-z0-9_-]*)\s+(?P<start>\d\d:\d\d)-(?P<end>\d\d:\d\d)\s+(?P<price>\d+(\.\d+)?)$")

Reading = tuple[datetime, int]          # (when, meter Wh)
Period = tuple[datetime, datetime]      # solar surplus on .. off


def _minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m):
        raise ValueError(f"bad time {hhmm}")
    return h * 60 + m


@dataclass(frozen=True)
class Band:
    name: str
    start: int      # minutes after local midnight
    end: int        # exclusive; 1440 = midnight
    price: Decimal


@dataclass(frozen=True)
class Tariff:
    bands: tuple[Band, ...]
    solar_price: Decimal | None = None
    tz: str = TZ
    # Flat amount per charge that delivers any energy (covers PayPal's fixed fee).
    session_fee: Decimal = Decimal(0)

    def __post_init__(self):
        edge = 0
        for b in self.bands:
            if b.start != edge or b.end <= b.start or b.price < 0:
                raise ValueError("tariff bands must run 00:00 to 24:00 in order, without gaps or overlaps")
            edge = b.end
        if edge != 1440:
            raise ValueError("tariff bands must end at 24:00")

    @classmethod
    def flat(cls, price: Decimal, solar_price: Decimal | None = None, session_fee: Decimal = Decimal(0)) -> Tariff:
        return cls((Band("standard", 0, 1440, Decimal(price)),), solar_price, session_fee=session_fee)

    @classmethod
    def parse(cls, spec: str, solar_price: Decimal | None = None, session_fee: Decimal = Decimal(0)) -> Tariff:
        bands = []
        for part in filter(None, (p.strip() for p in spec.split(";"))):
            m = _BAND.match(part)
            if not m:
                raise ValueError(f"tariff band {part!r} should look like 'peak 16:00-19:00 0.65'")
            bands.append(Band(m["name"], _minutes(m["start"]), _minutes(m["end"]), Decimal(m["price"])))
        return cls(tuple(bands), solar_price, session_fee=session_fee)

    def to_json(self) -> str:
        return json.dumps({
            "tz": self.tz,
            "solar_price": None if self.solar_price is None else str(self.solar_price),
            "bands": [[b.name, b.start, b.end, str(b.price)] for b in self.bands],
            "session_fee": str(self.session_fee),
        })

    @classmethod
    def from_json(cls, s: str) -> Tariff:
        d = json.loads(s)
        sp = d.get("solar_price")
        return cls(tuple(Band(n, a, b, Decimal(p)) for n, a, b, p in d["bands"]),
                   None if sp is None else Decimal(sp), d.get("tz", TZ), Decimal(d.get("session_fee", "0")))

    @property
    def max_price(self) -> Decimal:
        return max(b.price for b in self.bands)

    def band_at(self, at: datetime) -> Band:
        local = at.astimezone(ZoneInfo(self.tz))
        minute = local.hour * 60 + local.minute
        return next(b for b in self.bands if b.start <= minute < b.end)

    def rates(self) -> list[tuple[str, Decimal, list[str]]]:
        """[(band name, price, ["16:00-19:00", ...])] in first-appearance order, for display."""
        out: dict[str, tuple[Decimal, list[str]]] = {}
        for b in self.bands:
            span = f"{b.start // 60:02d}:{b.start % 60:02d}-{b.end // 60:02d}:{b.end % 60:02d}"
            out.setdefault(b.name, (b.price, []))[1].append(span)
        return [(n, p, spans) for n, (p, spans) in out.items()]

    def _boundaries(self, t0: datetime, t1: datetime) -> list[datetime]:
        tz = ZoneInfo(self.tz)
        d: date = t0.astimezone(tz).date() - timedelta(days=1)
        last = t1.astimezone(tz).date() + timedelta(days=1)
        out = []
        while d <= last:
            for b in self.bands:
                out.append(datetime.combine(d, time(b.start // 60, b.start % 60), tzinfo=tz).astimezone(timezone.utc))
            d += timedelta(days=1)
        return out

    def cost(self, readings: list[Reading], solar: list[Period] = ()) -> tuple[Decimal, dict[str, dict]]:
        """Cost of the energy between consecutive readings plus the session fee
        (only if any energy was delivered), unrounded, and a per-rate breakdown
        {name: {"wh": int, "price": str, "cost": str}}; the fee is the "fee" entry."""
        total = Decimal(0)
        parts: dict[str, list] = {}
        solar = [(a, b) for a, b in solar] if self.solar_price is not None else []
        for (t0, wh0), (t1, wh1) in zip(readings, readings[1:]):
            energy = Decimal(max(wh1 - wh0, 0))
            if not energy:
                continue
            if t1 <= t0:
                pieces = [(t0, t0, Decimal(1))]
            else:
                cuts = sorted({t0, t1, *(c for c in self._boundaries(t0, t1) if t0 < c < t1),
                               *(c for p in solar for c in p if c is not None and t0 < c < t1)})
                span = Decimal((t1 - t0).total_seconds())
                pieces = [(a, b, Decimal((b - a).total_seconds()) / span) for a, b in zip(cuts, cuts[1:])]
            for a, b, share in pieces:
                mid = a + (b - a) / 2
                if any(on <= mid < (off or datetime.max.replace(tzinfo=timezone.utc)) for on, off in solar):
                    name, price = SOLAR, self.solar_price
                else:
                    band = self.band_at(mid)
                    name, price = band.name, band.price
                e = energy * share
                c = e / 1000 * price
                total += c
                p = parts.setdefault(name, [Decimal(0), price, Decimal(0)])
                p[0] += e
                p[2] += c
        if total and self.session_fee:
            total += self.session_fee
            parts[FEE] = [Decimal(0), self.session_fee, self.session_fee]
        breakdown = {
            n: {"wh": int(e.to_integral_value(ROUND_HALF_UP)), "price": f"{pr}", "cost": f"{c.quantize(Decimal('0.01'), ROUND_HALF_UP)}"}
            for n, (e, pr, c) in parts.items()
        }
        return total, breakdown
