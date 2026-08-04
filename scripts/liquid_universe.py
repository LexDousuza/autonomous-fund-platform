"""
129 liquid NSE large/mid-caps across sectors (banking/financial services,
IT, energy/power, FMCG, auto, pharma, cement, metals, capital goods,
telecom, consumer durables/retail, chemicals, real estate, media),
cross-checked programmatically against config/instruments/
india_cash_universe.yaml's actual 2393-symbol list for exact spelling -
see docs/research/data/universe_expansion_2026-07-30.md for the
verification method and the 2 candidates dropped (TATAMOTORS - absent
from that universe due to Tata Motors' 2025 demerger; LTIM - no exact
match found, not guess-substituted).

Shared by every historical-bar fetch script (Groww, Angel One) so the
universe is defined once, not duplicated and left to drift between them.
"""
from __future__ import annotations

LIQUID_UNIVERSE_SYMBOLS = [
    # Banking/financial services
    "HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "INDUSINDBK",
    "BAJFINANCE", "BAJAJFINSV", "HDFCLIFE", "SBILIFE", "ICICIPRULI",
    "ICICIGI", "SBICARD", "CHOLAFIN", "PNB", "BANKBARODA", "CANBK",
    "IDFCFIRSTB", "AUBANK", "FEDERALBNK", "RBLBANK", "PFC", "RECLTD",
    "MUTHOOTFIN", "LICHSGFIN", "SHRIRAMFIN", "BAJAJHLDNG",
    # IT
    "TCS", "INFY", "WIPRO", "HCLTECH", "TECHM", "MPHASIS", "PERSISTENT",
    "COFORGE", "LTTS", "OFSS",
    # Energy/oil & gas/power
    "RELIANCE", "ONGC", "IOC", "BPCL", "GAIL", "NTPC", "POWERGRID",
    "ADANIGREEN", "TATAPOWER", "ADANIPOWER", "ADANIENSOL", "COALINDIA",
    "PETRONET", "HINDPETRO",
    # FMCG/consumer staples
    "HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "DABUR", "MARICO",
    "GODREJCP", "COLPAL", "TATACONSUM", "VBL", "UBL",
    # Auto
    "MARUTI", "M&M", "BAJAJ-AUTO", "HEROMOTOCO", "EICHERMOT", "TVSMOTOR",
    "ASHOKLEY", "BOSCHLTD", "MOTHERSON", "BALKRISIND",
    # Pharma
    "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "LUPIN", "AUROPHARMA",
    "TORNTPHARM", "ALKEM", "ZYDUSLIFE", "BIOCON", "GLAND",
    # Cement
    "ULTRACEMCO", "SHREECEM", "AMBUJACEM", "ACC", "DALBHARAT", "GRASIM",
    # Metals
    "TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "JINDALSTEL", "SAIL",
    "NMDC", "NATIONALUM",
    # Capital goods/industrials
    "LT", "SIEMENS", "ABB", "BEL", "BHEL", "CUMMINSIND", "HAVELLS",
    "POLYCAB", "THERMAX", "SKFINDIA",
    # Telecom
    "BHARTIARTL", "IDEA",
    # Consumer durables/retail
    "TITAN", "TRENT", "DMART", "PIDILITIND", "ASIANPAINT", "BERGEPAINT",
    "VOLTAS",
    # Chemicals
    "PIIND", "SRF", "UPL", "AARTIIND", "DEEPAKNTR",
    # Real estate
    "DLF", "GODREJPROP", "OBEROIRLTY",
    # Media
    "ZEEL", "PVRINOX", "SUNTV",
    # Conglomerates/ports
    "ADANIENT", "ADANIPORTS",
]
