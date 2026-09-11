"""Port interfaces (hexagonal architecture).

Every external dependency is expressed here as a ``Protocol``. The pipeline and
engines depend only on these; concrete Polymarket, Postgres and Redis code
lives in ``deepflow.adapters``. This is what makes the SDK swappable and the
engines testable without a network.
"""
