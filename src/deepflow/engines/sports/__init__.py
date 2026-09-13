"""Per-sport probability models.

One model per sport, never a shared generic one. The state variables that
determine a win probability are not transferable: a two-goal lead at minute 85
and a two-set lead in tennis are both "ahead", and they decay completely
differently. A generic model is a model that is wrong about every sport in a
different way.

## Two modules per sport, and what pairs with what

This package holds the *models*; :mod:`deepflow.engines.sports.rules` holds the
*feed interpretation*. They were built at different times and their filenames do
not line up, so the pairing is written down rather than inferred:

| Sport | Model here | Feed rules | State model |
| --- | --- | --- | --- |
| Soccer | ``football.py`` (`FootballEngine`) | ``rules/soccer.py`` | ``FootballState`` |
| American football | *none yet* | ``rules/gridiron.py`` | *none yet* |
| Tennis | ``tennis.py`` | ``rules/tennis.py`` | ``TennisState`` |
| Esports | *none yet* | ``rules/esports.py`` | *none yet* |
| Cricket | ``cricket.py`` | *none yet* | ``CricketState`` |
| Badminton | ``badminton.py`` | *none yet* | ``BadmintonState`` |

**"Football" here means soccer.** It follows
:attr:`deepflow.core.enums.MarketCategory.FOOTBALL`, which the API schema, the
database and the dashboard all use, so the name stays rather than being corrected
into a third spelling of the same sport. American football is ``gridiron`` in the
rules package and ``SportKind.AMERICAN_FOOTBALL`` in code, and
:data:`deepflow.engines.sports.rules.CATEGORY_BY_SPORT` is what keeps the two from
being mapped onto each other by name -- see its docstring for why that mapping is
load-bearing rather than cosmetic.

The two halves of the table that are ``none yet`` are not symmetrical: a missing
*rules* module means the sport cannot be read from the feed at all, while a missing
*model* means it can be read and not yet priced.
"""
