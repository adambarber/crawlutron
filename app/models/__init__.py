"""Every type this app declares, one concept per file.

The rule this package exists to enforce is that a declaration has exactly one
home, and that the home is findable from the name alone.

One concept per file, so the record a reader looks up is in the file named
for it. Not one *class* per file: ``taxonomy.py`` holds the classification
vocabulary because those declarations mutually reference each other, and
``crawl_result.py`` holds the strategy attempt plus the two records built over
it -- the render result and the engine result -- because splitting the family
put the same six-line vocabulary read in two files. The unit is the thing a
reader looks up, not the syntactic node.

NOTHING IS RE-EXPORTED HERE, and that is the load-bearing decision. An
``__init__.py`` that listed every type would make ``from app.models import
EngineCrawlResult`` work from anywhere -- and would therefore let a type live in
the wrong file while still importing cleanly, which is exactly the drift the
typing program was opened to remove. It would also be a hand-maintained list
mirroring what the tree contains, which is a declaration that goes stale the
moment anyone adds a file and forgets a line. Every import names its file:

    from app.models.crawl_result import EngineCrawlResult

Consequences worth knowing before adding to this package:

- Names here are PUBLIC. A declaration crossing a package boundary is a private
  name crossing one, and ``tests/private_import_ledger.py`` treats that as a
  violation. Helpers stay private to their own file; declarations are named for
  what they declare.
- Wire records live here too. A pydantic response model is a projection of an
  internal record, and keeping it outside this package is what left
  ``DiscoveredLink`` and ``PageMetadata`` each declared twice with nothing
  connecting the two forms. A projection sits beside its owner at
  ``<stem>_response.py``, which is how the mirror invariant in
  ``tests/invariants/test_result_shape.py`` finds the pair.
- A module holding exactly one declaration is named for it, modulo a trailing
  ``Error``. A module holding several is named for the family -- ``crawl_worker.py``
  holds the sync and async forms of one worker -- and both are enforced by
  ``tests/invariants/test_public_surface.py``: one declaration in two files fails
  as its own offence, because that is the drift this split was for and nothing
  else here would notice it.
- Nothing may be re-exported from this file, and
  ``test_models_package_reexports_nothing`` is why the docstring is the only thing
  here: an import would make a type findable from anywhere, which is the thing the
  one-home rule exists to prevent.
"""
