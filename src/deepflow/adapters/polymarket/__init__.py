"""Polymarket integration.

This package is the *only* place in the codebase permitted to import from the
``polymarket`` SDK. Everything above it speaks ``deepflow.core.domain``.

Verified against ``polymarket-client`` 0.10.0:

* ``AsyncPublicClient``  -- unauthenticated reads, public subscriptions
* ``AsyncSecureClient``  -- authenticated trading, account state, user stream
* Subscription specs live in ``polymarket.streams`` (``MarketSpec``,
  ``SportsSpec``, ``CryptoPricesSpec``, ``UserSpec``, ...)

Note on the legacy client: ``py-clob-client`` is archived upstream and
documented as non-functional. It must not be reintroduced.
"""
