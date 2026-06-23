"""Destination-country UTC offsets for the diurnal traffic shaper.

The shaper anchors a campaign's 24h curve to GMT (``time.gmtime``); ``tz_offset``
then rotates the curve to the destination market's local daypart so the peak
lands on that market's business hours. Rather than have an operator type the
offset, it is derived automatically from the node's drop zone -> country here.

Whole-hour offsets only: ``LoopPreset.tz_offset`` is an INTEGER and
``traffic_profile.make_curve`` rotates the curve by whole hours, so half-hour
markets (Iran +3:30, Afghanistan +4:30, India +5:30) are intentionally omitted
and fall back to the caller-supplied value. Offsets are fixed (no DST); markets
that observe DST/Ramadan shifts (e.g. Morocco, Lebanon) use their standard-time
offset — set the preset's manual ``tz_offset`` to override when needed.
"""
from typing import Optional

# Lowercased country label -> standard UTC offset in whole hours. Keyed off the
# label that gen_loop_csv.derive_country() produces from a node's drop zone.
COUNTRY_UTC_OFFSET = {
    # ── Maghreb / North Africa ──
    "morocco": 1, "algeria": 1, "tunisia": 1, "libya": 2, "egypt": 2,
    "mauritania": 0, "western sahara": 1,
    # ── Middle East / Gulf ──
    "iraq": 3, "saudi arabia": 3, "kuwait": 3, "qatar": 3, "bahrain": 3,
    "yemen": 3, "jordan": 3, "syria": 3, "lebanon": 2, "palestine": 2,
    "israel": 2, "turkey": 3, "oman": 4, "pakistan": 5,
    "united arab emirates": 4, "uae": 4,
    # ── West Africa ──
    "senegal": 0, "gambia": 0, "guinea": 0, "guinea-bissau": 0,
    "sierra leone": 0, "liberia": 0, "ivory coast": 0, "cote d'ivoire": 0,
    "cote d’ivoire": 0, "mali": 0, "burkina faso": 0, "togo": 0, "ghana": 0,
    "nigeria": 1, "niger": 1, "benin": 1, "chad": 1, "cameroon": 1,
    "cape verde": -1,
    # ── Central / East Africa ──
    "sudan": 2, "south sudan": 2, "ethiopia": 3, "eritrea": 3, "somalia": 3,
    "kenya": 3, "tanzania": 3, "uganda": 3, "rwanda": 2, "burundi": 2,
    "djibouti": 3, "democratic republic of congo": 1, "congo": 1,
    # ── Europe (standard time) ──
    "united kingdom": 0, "uk": 0, "portugal": 0, "ireland": 0,
    "france": 1, "spain": 1, "italy": 1, "germany": 1, "netherlands": 1,
    "belgium": 1, "switzerland": 1, "romania": 2, "greece": 2,
}


def country_utc_offset(country: Optional[str]) -> Optional[int]:
    """Whole-hour UTC offset for a destination country label, or ``None`` if
    unknown. Case- and whitespace-insensitive."""
    if not country:
        return None
    return COUNTRY_UTC_OFFSET.get(country.strip().lower())


def offset_for_zone(dest_zone: Optional[str], fallback: int = 0,
                    country: Optional[str] = None) -> int:
    """Auto ``tz_offset`` for a node's drop zone.

    ``country`` wins when given (DB-overlay zones carry an explicit country);
    otherwise the country is derived from ``dest_zone`` via
    ``gen_loop_csv.derive_country``. Returns ``fallback`` (the preset's manual
    tz_offset) when the zone is blank or the country is not in the table.
    """
    if country is None:
        if not dest_zone:
            return fallback
        from gencall.scripts.gen_loop_csv import derive_country
        country = derive_country(dest_zone)
    off = country_utc_offset(country)
    return off if off is not None else fallback
