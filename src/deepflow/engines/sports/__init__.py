"""Per-sport probability models.

One model per sport, never a shared generic one. The state variables that
determine a win probability are not transferable: a two-goal lead at minute 85
and a two-set lead in tennis are both "ahead", and they decay completely
differently. A generic model is a model that is wrong about every sport in a
different way.
"""
