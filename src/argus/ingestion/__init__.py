"""L1 — provider-agnostic ingestion and adapter layer.

Binance public spot/USD-M REST and WebSocket records are converted into typed crypto
contracts; the older ccxt/BIST/news adapters stay backward compatible. There are no
private order endpoints.
"""

from argus.ingestion.base import MarketDataAdapter
from argus.ingestion.binance import (
    BinanceRESTProvider,
    BinanceWebSocketParser,
    BinanceWebSocketProvider,
    JSONTransport,
    OrderBookReconciler,
    RateLimiter,
    ReconnectPolicy,
    WebSocketSupervisor,
)
from argus.ingestion.borsapy_kap import BorsapyKAPProvider, kap_adapter, kap_provider
from argus.ingestion.borsapy_provider import BorsapyProvider, bist_adapter, borsapy_provider
from argus.ingestion.crypto import (
    CryptoAdapter,
    ExchangeClient,
    binance_adapter,
    ccxt_symbol,
    normalize_crypto_symbol,
)
from argus.ingestion.news import DisclosureSource, KAPAdapter, KAPDisclosureProvider
from argus.ingestion.news_intelligence import (
    ConfiguredNewsApiProvider,
    NewsProvider,
    NoDataConfiguredNewsApiProvider,
    OfficialAnnouncementProvider,
    RssAtomProvider,
    RssFeedSource,
)
from argus.ingestion.openbb import (
    GlobalDataProvider,
    OpenBBAdapter,
    OpenBBProvider,
    openbb_adapter,
)
from argus.ingestion.tr import BISTDataProvider, TRAdapter

__all__ = [
    "BISTDataProvider",
    "BinanceRESTProvider",
    "BinanceWebSocketParser",
    "BinanceWebSocketProvider",
    "BorsapyKAPProvider",
    "BorsapyProvider",
    "ConfiguredNewsApiProvider",
    "CryptoAdapter",
    "DisclosureSource",
    "ExchangeClient",
    "GlobalDataProvider",
    "JSONTransport",
    "KAPAdapter",
    "KAPDisclosureProvider",
    "MarketDataAdapter",
    "NewsProvider",
    "NoDataConfiguredNewsApiProvider",
    "OfficialAnnouncementProvider",
    "OpenBBAdapter",
    "OpenBBProvider",
    "OrderBookReconciler",
    "RateLimiter",
    "ReconnectPolicy",
    "RssAtomProvider",
    "RssFeedSource",
    "TRAdapter",
    "WebSocketSupervisor",
    "binance_adapter",
    "bist_adapter",
    "borsapy_provider",
    "ccxt_symbol",
    "kap_adapter",
    "kap_provider",
    "normalize_crypto_symbol",
    "openbb_adapter",
]
