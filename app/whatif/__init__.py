"""What-if scenario simulations (Step AL).

Pure-function engine: run a hypothetical disruption against a twin
snapshot and get impact + a recovery plan, without touching live
operations. See ``app/whatif/simulator.py``.
"""

__all__ = ["simulator", "adapter", "drills", "rehearsal", "service"]
