"""Currency rules and decimal amount parsing for the ledger."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

DEFAULT_CURRENCY = "SGD"
_ZERO_DECIMAL = {"JPY", "KRW", "VND"}
_ISO_CURRENCIES = set(
    "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD "
    "BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB "
    "EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK "
    "JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK "
    "MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR "
    "PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STN SVC "
    "SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF "
    "XAG XAU XBA XBB XBC XBD XCD XDR XOF XPD XPF XPT XSU XUA XXX YER ZAR ZMW ZWG ZWL".split()
)
_EXPONENTS = {code: 0 if code in _ZERO_DECIMAL else 2 for code in _ISO_CURRENCIES}
_CURRENCY_SYMBOLS = {"SGD": "S$", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "KRW": "₩"}


_COUNTRY_CURRENCIES = {
    "JP": "JPY",
    "JAPAN": "JPY",
    "KR": "KRW",
    "KOREA": "KRW",
    "SOUTH KOREA": "KRW",
    "TH": "THB",
    "THAILAND": "THB",
    "VN": "VND",
    "VIETNAM": "VND",
    "TW": "TWD",
    "TAIWAN": "TWD",
    "MY": "MYR",
    "MALAYSIA": "MYR",
    "ID": "IDR",
    "INDONESIA": "IDR",
    "HK": "HKD",
    "HONG KONG": "HKD",
    "CN": "CNY",
    "CHINA": "CNY",
    "US": "USD",
    "UNITED STATES": "USD",
    "UNITED STATES OF AMERICA": "USD",
    "GB": "GBP",
    "UNITED KINGDOM": "GBP",
    "AU": "AUD",
    "AUSTRALIA": "AUD",
    "SG": "SGD",
    "SINGAPORE": "SGD",
    "PH": "PHP",
    "PHILIPPINES": "PHP",
}
_EU_COUNTRIES = {
    "AT": "Austria",
    "BE": "Belgium",
    "BG": "Bulgaria",
    "HR": "Croatia",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DK": "Denmark",
    "EE": "Estonia",
    "FI": "Finland",
    "FR": "France",
    "DE": "Germany",
    "GR": "Greece",
    "HU": "Hungary",
    "IE": "Ireland",
    "IT": "Italy",
    "LV": "Latvia",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "MT": "Malta",
    "NL": "Netherlands",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "SK": "Slovakia",
    "SI": "Slovenia",
    "ES": "Spain",
    "SE": "Sweden",
}
for _code, _country in _EU_COUNTRIES.items():
    _COUNTRY_CURRENCIES[_code] = "EUR"
    _COUNTRY_CURRENCIES[_country.upper()] = "EUR"


def currency_exponent(currency: str) -> int:
    code = currency.upper()
    if code not in _ISO_CURRENCIES:
        raise ValueError(f"Unknown ISO currency code: {currency}")
    return _EXPONENTS[code]


def currency_symbol(currency: str) -> str:
    return _CURRENCY_SYMBOLS.get(currency.upper(), currency.upper())


def parse_amount(value: str, currency: str) -> int:
    """Parse a human decimal string into exact minor units without float arithmetic."""
    exponent = currency_exponent(currency)
    cleaned = value.strip()
    if re.search(r"[^0-9.,+-]", cleaned):
        raise ValueError(
            "Enter the amount without a currency symbol; pass currency as a "
            "separate currency argument."
        )
    if not re.fullmatch(r"[+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", cleaned):
        raise ValueError("Enter a valid decimal amount, such as 1,234.50.")
    cleaned = cleaned.replace(",", "")
    try:
        decimal = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Enter a valid decimal amount, such as 12.50.") from exc
    if not decimal.is_finite() or decimal <= 0:
        raise ValueError("Amount must be a positive decimal number.")
    scaled = decimal * (10**exponent)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{currency.upper()} supports {exponent} decimal places.")
    return int(scaled)


def format_amount(amount: int, currency: str) -> str:
    exponent = currency_exponent(currency)
    value = Decimal(amount) / (10**exponent)
    return f"{currency_symbol(currency)}{value:.{exponent}f} {currency.upper()}"


def currency_for_country(country: str) -> str:
    normalized = country.strip().upper()
    try:
        return _COUNTRY_CURRENCIES[normalized]
    except KeyError as exc:
        raise ValueError(f"No currency mapping for destination {country!r}.") from exc


def resolve_currency(
    explicit: str | None, trip_currencies: list[str] | tuple[str, ...] | None, *, trip_active: bool
) -> str:
    if explicit:
        currency_exponent(explicit)
        return explicit.upper()
    currencies = tuple(dict.fromkeys(code.upper() for code in trip_currencies or ()))
    if trip_active and len(currencies) == 1:
        return currencies[0]
    if not trip_active:
        return DEFAULT_CURRENCY
    if len(currencies) > 1:
        raise ValueError(f"Which currency? This trip uses: {', '.join(currencies)}.")
    raise ValueError("This trip has no currency; specify a currency explicitly.")
