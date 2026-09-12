"""Polymarket integration.

This package is the *only* place in the codebase permitted to import from the
``polymarket`` SDK. Everything above it speaks ``deepflow.core.domain``.

The one exception to "nothing above imports from here" is :mod:`venue`, which
holds published exchange rules (fee formula, tick grid, contract addresses,
lifetime constraints) and depends on nothing. Engines may import it.

Verified against ``polymarket-client`` 0.10.0:

* ``AsyncPublicClient``  -- unauthenticated reads, public subscriptions
* ``AsyncSecureClient``  -- authenticated trading, account state, user stream
* Subscription specs live in ``polymarket.streams`` (``MarketSpec``,
  ``SportsSpec``, ``CryptoPricesSpec``, ``CryptoPricesChainlinkTwapSpec``,
  ``EquityPricesSpec``, ``CommentsSpec``, ``UserSpec``)

``subscribe()`` takes a *list* of specs, must be awaited, and returns an async
context manager that yields one merged event stream -- not a stream per spec::

    async with await client.subscribe([MarketSpec(token_ids=[...]), UserSpec()]) as stream:
        async for event in stream:
            ...  # discriminate on event.topic, then event.type

Errors derive from ``PolymarketError``; ``RateLimitError``, ``UserInputError``
and ``RequestRejectedError`` (carrying ``.status``) are the ones worth catching
by name. List endpoints return a paginator, not a coroutine: ``pages =
client.list_markets(closed=False)`` then ``await pages.first_page()`` or ``async
for item in pages.iter_items()``.

Note on the legacy client: ``py-clob-client`` is archived upstream and
documented as non-functional. It must not be reintroduced.
"""
