"""URL admission layer: may this page be fetched at all.

``read_policy`` is the one read edge; whole-site and recursive admission are
separate modules on purpose. The paced-admission kernel is a separate ``admission`` package.
"""
