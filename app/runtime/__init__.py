"""What this process is doing, and how much room it has: ``metrics`` (counts work),
``logging_config`` (one JSON logger), ``pressure_sizing`` (asks ``cgroup_sense`` +
``pressure_memory``), ``process_state``. Process-wide, so layers consult them, never own them.
"""
