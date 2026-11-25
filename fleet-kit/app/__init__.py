"""Agent Fleet Hub - Stage 0 deliverable #1: runnable minimal technical prototype.

Modular monolith (plan v0.2.6 §4): six logical modules as packages, one
deployable process. Persistence is SQLite here (dev, no Docker on this host);
the storage layer is a thin interface so PostgreSQL can replace it without
touching the modules. The message bus is in-process with the same interface
the Valkey/Redis-backed bus will implement.
"""
