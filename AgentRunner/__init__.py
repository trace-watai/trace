"""TRACE target-agent runner (refund/support vertical slice).

A minimal agent loop: a fixture model yields actions, a mock tool runs them, and
every step is emitted to a trace sink. Model and trace are injectable so real
implementations drop in without changing the loop. See README.md.
"""

__version__ = "0.1.0"
