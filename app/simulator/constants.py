"""Shared constants for the passenger-journey simulator. Split out from
live_simulator.py so lightweight callers (e.g. app/api/v1/users.py,
which just needs to filter these accounts out of an admin list) don't
have to import the whole simulator module and its heavier dependencies
(websocket manager, AI prediction engine, etc).
"""

SIMULATED_EMAIL_DOMAIN = "sim.metroflow.internal"
