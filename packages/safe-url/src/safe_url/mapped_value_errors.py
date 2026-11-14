"""safe_url is stateless: NormalizedUrl.__init__ touches no shared module state.

The mapped foreign-ValueError tally lived here; it is removed so construction
is a pure admission boundary with no record side effect.
"""
