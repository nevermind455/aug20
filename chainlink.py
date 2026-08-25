"""Disabled legacy Chainlink spot adapter.

The Ethereum BTC/USD aggregator is a spot feed. It is not the Chainlink
60-second TWAP named by BTC five-minute Up/Down market rules. This module is
kept only so an old import fails explicitly instead of silently bringing the
wrong oracle back into the decision path. Use ``chainlink_strike.ChainlinkStrike``.
"""


def get_chainlink_btc_price(rpc_url=None):
    del rpc_url
    raise RuntimeError(
        "legacy Chainlink spot price is disabled; use the RTDS 60-second TWAP")
