"""Representative Polymarket API payload fixtures for testing."""

ASSET_ID = "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
MARKET_ID = "0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
WALLET_ADDRESS = "0xDeadBeef1234567890AbCdEf1234567890AbCdEf"
TX_HASH = "0xfeedface1234567890abcdef1234567890abcdef1234567890abcdef1234567890"

# Polymarket market WebSocket last_trade_price event
WS_LAST_TRADE_PRICE = {
    "event_type": "last_trade_price",
    "asset_id": ASSET_ID,
    "price": "0.41",
    "size": "54878.05",
    "timestamp": "1704067200.123456",
}

# Polymarket market WebSocket price_change event (order book level update)
WS_PRICE_CHANGE = {
    "event_type": "price_change",
    "timestamp": "1704067200.123456",
    "changes": [
        {
            "asset_id": ASSET_ID,
            "price": "0.41",
            "side": "BUY",
            "size": "54878.05",
        }
    ],
}

# Polymarket market WebSocket book event (not a trade signal — order book snapshot)
WS_BOOK_EVENT = {
    "event_type": "book",
    "asset_id": ASSET_ID,
    "market": MARKET_ID,
    "timestamp": "1704067200.000000",
    "hash": "0xhash",
    "bids": [["0.40", "10000"], ["0.39", "25000"]],
    "asks": [["0.42", "8000"], ["0.43", "15000"]],
}

# CLOB API /trades response — single page with one matching trade
CLOB_TRADES_RESPONSE = {
    "next_cursor": "LTE=",
    "data": [
        {
            "id": "trade-id-001",
            "taker_order_id": "order-id-001",
            "market": MARKET_ID,
            "asset_id": ASSET_ID,
            "side": "BUY",
            "size": "54878.05",
            "fee_rate_bps": "0",
            "price": "0.41",
            "status": "CONFIRMED",
            "match_time": "1704067200",
            "last_update": "1704067201",
            "outcome": "Yes",
            "bucket_index": 0,
            "owner": WALLET_ADDRESS,
            "maker_address": WALLET_ADDRESS,
            "transaction_hash": TX_HASH,
            "maker_orders": [],
        }
    ],
}

# CLOB API /trades response — empty (no match)
CLOB_TRADES_EMPTY = {
    "next_cursor": "LTE=",
    "data": [],
}

# Gamma API markets response — single active binary market
GAMMA_MARKETS_RESPONSE = [
    {
        "id": MARKET_ID,
        "question": "Will the S&P 500 close above 5000 on January 31, 2025?",
        "conditionId": MARKET_ID,
        "slug": "sp500-above-5000-jan-31-2025",
        "description": "Resolves YES if the S&P 500 closes above 5000.",
        "startDate": "2024-01-01T00:00:00Z",
        "endDate": "2025-01-31T23:59:59Z",
        "active": True,
        "closed": False,
        "archived": False,
        "resolved": False,
        "resolution": None,
        "tokens": [
            {"token_id": ASSET_ID, "outcome": "Yes"},
            {"token_id": "0xNO000000000000000000000000000000000000000000000000000000000000", "outcome": "No"},
        ],
    }
]
