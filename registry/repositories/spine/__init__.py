"""Spine replica adapter package (add-supply-store-unification, tasks 2.1-2.4).

The registry execution plane over the facet-side supply spine store:

- ``replica``          — local read-only SQLite replica overwritten only by pulls.
- ``projection_client``— pull client for the spine read-only projection endpoint.
- ``write_gate``       — forward-write client for the spine write gate.
- ``actor_context``    — contextvar passthrough of the acting identity.
- ``mapping``          — spine projection rows <-> legacy Mongo document shapes.
- ``server_repository``/``skill_repository`` — the ServerRepositoryBase /
  SkillRepositoryBase implementations wired in by the factory when
  ``SPINE_REPLICA_MODE=1``.
- ``sync_scheduler``   — periodic puller on the registry's refresh cadence.

The spine side (facet gateway ``supply-store.js``) owns the physical store and
its contract; nothing in this package may widen or restate it.
"""
