"""What this process is doing, and how much room it has: ``metrics`` (counts work),
``logging_config`` (one JSON logger), ``resource_pressure`` (asks ``cgroup_sense`` +
``memory_pressure``), ``process_state``. Process-wide, so layers consult them, never own them.
"""
