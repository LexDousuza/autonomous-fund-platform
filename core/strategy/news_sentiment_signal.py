from __future__ import annotations

from core.news.google_news_feed import GoogleNewsFeed
from core.news.sentiment import score_headlines
from core.strategy.base import (
    MarketBar,
    SignalSide,
    Strategy,
    StrategyContext,
    TradeSignal,
)

_DEFAULT_REFRESH_INTERVAL_BARS = 12
_DEFAULT_BUY_SCORE_THRESHOLD = 2
_DEFAULT_MAX_HEADLINES = 10
_DEFAULT_QUERY_TEMPLATE = "{symbol} NSE share price"


class NewsSentimentSignalStrategy(Strategy):
    """
    V1 news-driven entry signal: buys a symbol when its recent headline
    coverage is net-bullish by a keyword-count score (see
    core/news/sentiment.py).

    Hypothesis: a burst of net-positive news coverage precedes or
    accompanies short-term price strength worth entering on. Not tested
    against real historical news+price data - see the caveat below.

    Params supported via StrategyContext.parameters:
    - refresh_interval_bars (int, default 12): minimum bars between news
      checks for the same symbol - avoids hitting the news feed every
      bar. News relevance doesn't change bar-to-bar; this just rate-limits
      the fetch.
    - buy_score_threshold (int, default 2): minimum score_headlines()
      result to trigger a BUY. Score is a raw net keyword count (e.g. 3
      positive keywords - 1 negative = 2), not a normalized probability.
    - max_headlines (int, default 10): headlines fetched per check.
    - query_template (str, default "{symbol} NSE share price"): formatted
      with the bar's symbol to build the news search query.
    - news_feed: an object with a
      ``fetch(query: str, max_items: int) -> list[NewsItem]`` method
      (see core.news.google_news_feed.GoogleNewsFeed). Required - not a
      YAML-serializable primitive, so this must be injected by whatever
      builds the StrategyContext (an app's session-building code), not
      loaded from config/strategies/*.yaml.

    Entry only, no stop-loss/exit logic here - like
    SimpleBreakoutMomentumStrategy, exits are the platform's shared exit
    engine's job (core/portfolio/exits.py via ExitPolicyConfig), not a
    per-strategy concern.

    Assumptions / known limitations:
    - The keyword scorer is a naive bag-of-words heuristic (see
      core/news/sentiment.py's own caveat) - no negation handling, no
      real language understanding.
    - This strategy is NOT meaningfully backtestable against this
      platform's synthetic or CSV historical data: on_bar performs a real
      network call to a live news feed, so a "backtest" would just be
      today's real headlines replayed against unrelated historical/
      synthetic price bars, not headlines matching the bars' actual
      dates. Only run this in paper/live sessions, not
      apps/backtester/run_backtest.py.
    - The query is symbol-based only ("RELIANCE NSE share price"), not
      the company's full name - Google News generally matches ticker
      mentions reasonably well, but this is a known imprecision, not a
      verified-accurate mapping.

    Future improvements: replace the keyword scorer with a real
    sentiment/LLM classifier; weight headlines by recency; dedupe
    near-identical headlines across sources before scoring.
    """

    def __init__(self, context: StrategyContext) -> None:
        super().__init__(context)
        self._last_checked_bar_count: dict[str, int] = {}

    def on_bar(
        self,
        bar: MarketBar,
        history: list[MarketBar],
        open_positions: dict[str, object],
    ) -> list[TradeSignal]:
        params = self.context.parameters or {}

        refresh_interval_bars = int(params.get("refresh_interval_bars", _DEFAULT_REFRESH_INTERVAL_BARS))
        buy_score_threshold = int(params.get("buy_score_threshold", _DEFAULT_BUY_SCORE_THRESHOLD))
        max_headlines = int(params.get("max_headlines", _DEFAULT_MAX_HEADLINES))
        query_template = str(params.get("query_template", _DEFAULT_QUERY_TEMPLATE))
        news_feed = params.get("news_feed") or GoogleNewsFeed()

        if refresh_interval_bars <= 0:
            raise ValueError("refresh_interval_bars must be >= 1")

        position_key = f"{bar.exchange}:{bar.symbol}"
        if position_key in open_positions:
            return []

        current_bar_count = len(history) + 1  # + this bar itself
        last_checked = self._last_checked_bar_count.get(position_key)
        if last_checked is not None and (current_bar_count - last_checked) < refresh_interval_bars:
            return []

        self._last_checked_bar_count[position_key] = current_bar_count

        query = query_template.format(symbol=bar.symbol)
        items = news_feed.fetch(query, max_items=max_headlines)
        score = score_headlines(items)

        if score < buy_score_threshold:
            return []

        return [
            TradeSignal(
                strategy_name=self.context.strategy_name,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side=SignalSide.BUY,
                timestamp=bar.timestamp,
                reason="net_bullish_news_coverage",
                confidence=None,
                metadata={
                    "news_score": score,
                    "buy_score_threshold": buy_score_threshold,
                    "headline_count": len(items),
                    "headlines": [item.headline for item in items],
                },
            )
        ]


from core.strategy.registry import registry

registry.register(
    "news_sentiment_signal",
    NewsSentimentSignalStrategy,
)
