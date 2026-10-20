"""What happens next: the orchestration layer decides *what to do*, not doing it.

The engine admits nothing (that is ``app.admission``) and fetches nothing (``app.transport``);
``strategy_chain`` decides strategy order; ``engine_result``/``render_result`` are the output boundary.
"""
