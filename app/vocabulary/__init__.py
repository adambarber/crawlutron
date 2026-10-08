"""Vocabulary: the names this service uses, one topic per module.

Every layer imports from here -- ``app.models``, ``app.api``, the crawl modules,
``app.discovery``, ``app.mcp`` -- so it is the tree's foundation and gets one
nameable home rather than eight sibling modules a reader has to choose between. A
module here holds ONE topic: the classification taxonomy, the strategy names, the
limits, the browser and request-profile vocabularies, the host predicates, the
extraction methods, or the service identity.

This package exists because ``app/constants.py`` held all of it in one file, which
made "where does X live" answerable only by reading the whole tree.
"""
