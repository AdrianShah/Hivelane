"""Local companion process: manages watcher subprocesses for the dashboard.

Runs only on 127.0.0.1 — it spawns local processes, so it must never be
reachable from the network.
"""
