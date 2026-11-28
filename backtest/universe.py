"""Broad, cross-sector liquid US equities universe for Mode A point-in-time screening.

Intentionally spans all 11 GICS sectors (including cyclicals, financials, industrials,
energy, healthcare, consumer, and mid/large-cap growth) rather than only recent winners
to reduce hindsight bias during Mode A historical screening.
"""

from __future__ import annotations

from typing import List

# ~240 liquid US-listed NASDAQ/NYSE equities across all major sectors
LIQUID_US_UNIVERSE: List[str] = [
    # Mega-Cap Tech & Software
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "ORCL", "CRM",
    "ADBE", "CSCO", "INTU", "NOW", "PANW", "CRWD", "SNOW", "DDOG", "NET", "ZS",
    "PLTR", "APP", "UBER", "NFLX", "SHOP", "SQ", "PYPL", "TTD", "TEAM", "HUBS",
    "WDAY", "FTNT", "OKTA", "MDB", "CFLT", "DOCU", "TWLO", "U", "PATH", "IOT",
    "DUOL", "RDDT", "RBLX", "PINS", "SNAP", "SPOT", "ROKU", "ZM", "ESTC", "GTLB",
    # Semiconductors & Hardware
    "AMD", "INTC", "QCOM", "TXN", "AMAT", "LRCX", "KLAC", "MU", "MRVL", "ADI",
    "NXPI", "ON", "MCHP", "MPWR", "ARM", "SMCI", "ANET", "DELL", "HPE", "HPQ",
    "WDC", "STX", "PSTG", "NTAP", "SMTC", "LSCC", "TER", "ENTG", "SWKS", "QRVO",
    # Financials & Fintech
    "JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW", "BLK", "BX", "KKR",
    "APO", "AXP", "V", "MA", "COF", "DFS", "USB", "PNC", "TFC", "FITB",
    "COIN", "HOOD", "SOFI", "AFRM", "UPST", "NU", "IBKR", "CME", "ICE", "SPGI",
    "PGR", "ALL", "TRV", "MET", "PRU", "AFL", "CB", "AIG",
    # Healthcare & Biotech
    "LLY", "UNH", "JNJ", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "AMGN",
    "GILD", "VRTX", "REGN", "BIIB", "ISRG", "SYK", "BSX", "MDT", "EW", "DXCM",
    "PODD", "ALGN", "HCA", "CVS", "CI", "ELV", "HUM", "CNC", "MRNA", "BNTX",
    "HIMS", "EXAS", "NTRA", "CRSP", "ILMN", "ZTS", "IDXX",
    # Industrials, Aerospace, Defense & Power
    "GE", "GEV", "CAT", "DE", "HON", "UNP", "CSX", "NSC", "UPS", "FDX",
    "BA", "LMT", "RTX", "NOC", "GD", "LHX", "AXON", "TDG", "HWM", "ETN",
    "EMR", "PH", "ROK", "URI", "PWR", "VRT", "TT", "CARR", "JCI", "WM",
    "RSG", "DAL", "UAL", "AAL", "LUV", "RCL", "CCL", "NCLH",
    # Energy & Materials
    "XOM", "CVX", "COP", "EOG", "SLB", "HAL", "BKR", "OXY", "DVN", "FANG",
    "MPC", "VLO", "PSX", "KMI", "WMB", "OKE", "LNG", "EQT", "CTRA", "APA",
    "FCX", "NEM", "AA", "NUE", "STLD", "CLF", "X", "LIN", "APD", "SHW",
    "DD", "DOW", "ALB", "MP", "CCJ",
    # Consumer Discretionary & Retail
    "HD", "LOW", "COST", "WMT", "TGT", "DG", "DLTR", "BJ", "TJX", "ROST",
    "NKE", "LULU", "DECK", "ONON", "CROX", "BIRK", "ANF", "URBN", "GAP", "AEO",
    "SBUX", "MCD", "CMG", "CAVA", "BROS", "DPZ", "YUM", "QSR", "WING", "SHAK",
    "ABNB", "BKNG", "EXPE", "MAR", "HLT", "MGM", "LVS", "WYNN", "DKNG", "FLUT",
    "F", "GM", "RIVN", "LCID", "CVNA", "KMX", "AZO", "ORLY",
    # Consumer Staples & Utilities & Telecom & REITs
    "PG", "KO", "PEP", "MNST", "CELH", "PM", "MO", "MDLZ", "KHC", "GIS",
    "CL", "KMB", "EL", "STZ", "TAP",
    "NEE", "CEG", "VST", "NRG", "DUK", "SO", "D", "AEP", "EXC", "PCG",
    "T", "VZ", "TMUS", "DIS", "CMCSA", "CHTR", "WBD", "PARA",
    "AMT", "PLD", "EQIX", "DLR", "O", "SPG", "VICI",
    # Crypto / High-Beta Momentum / Thematic
    "MSTR", "MARA", "RIOT", "CLSK", "IREN", "RKLB", "ASTS", "IONQ", "SOUN", "BBAI",
]


def get_liquid_us_universe() -> List[str]:
    """Returns deduplicated ordered list of liquid US equities for Mode A screening."""
    seen = set()
    out: List[str] = []
    for sym in LIQUID_US_UNIVERSE:
        u = sym.strip().upper()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out
