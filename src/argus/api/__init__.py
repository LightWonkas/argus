"""L6 Dashboard — backend of the read-only web cockpit.

A **read/presentation** layer on top of the existing services
(storage/observability/platform); no new data sources. FastAPI is imported lazily, only
inside :mod:`argus.api.app`. Security (password hashing + sessions) lives in
:mod:`argus.api.security`, pure and network-free.
"""

from __future__ import annotations

from argus.api.security import hash_password

__all__ = ["hash_password"]
