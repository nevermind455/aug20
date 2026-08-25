"""Pooled HTTP for venue reads.

Every venue call used to go through ``requests.get``, which builds a fresh
connection and throws it away again. Measured against the live CLOB on this
machine's link that cost 415ms per request versus 95ms once pooled - roughly
320ms of pure TCP+TLS handshake on every single call, and one more independent
chance to time out or be reset. At thirteen calls per six-second trade cycle
that was 5.4s of the cycle spent shaking hands.

Sessions are per-thread. The bot issues these reads from ``asyncio.to_thread``
workers, and ``requests.Session`` is not documented as thread-safe; one session
per worker keeps the connection reuse without sharing mutable state.

Callers go through the module-level ``get`` so there is exactly one place to
stub in tests, and one place to change if pooling ever needs tuning.
"""
from __future__ import annotations

import threading

import requests

_local = threading.local()


def session() -> requests.Session:
    """The calling thread's session, created on first use."""
    existing = getattr(_local, "session", None)
    if existing is not None:
        return existing
    created = requests.Session()
    _local.session = created
    return created


def get(url, **kwargs):
    """GET through this thread's pooled connection.

    Deliberately a thin passthrough: same arguments and same exceptions as
    ``requests.get``, so call sites and their error handling are unchanged.
    """
    return session().get(url, **kwargs)


def close() -> None:
    """Drop this thread's session. Only needed by tests and shutdown paths."""
    existing = getattr(_local, "session", None)
    if existing is not None:
        try:
            existing.close()
        finally:
            _local.session = None
