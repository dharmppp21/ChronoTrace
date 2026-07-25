"""The HTTP surface between the Python engine and a browser -- and one hard rule it enforces.

**A storage type never reaches the wire.** `server` serialises `dto.py`'s explicit wire
types and nothing else: never a `ProgramState`, `Delta`, `CapturedValue`, `Event`, `Keyframe`
or any other type from `store`/`recorder`/`reconstruct`/`index`. This is not fussiness -- it
is the dependency arrow the project has enforced for 31 days, applied to its most dangerous
boundary. Leak a storage type here and the on-disk format *becomes* the wire format *becomes*
the public API: three things that must stay free to change, welded into one that cannot.

The wire types are **derived from the five screens**, not from the engine's capabilities. An
API designed from what the engine can do leaks the engine; an API designed from what the
timeline scrubber, source pane, variable panel, call tree and query box actually render stays
small. Each endpoint (ADR-0010) maps to one screen, one query it drives, and a latency budget.

What this layer owns
--------------------
The HTTP contract: routing, the DTOs, caching (immutable recordings get strong ETags, a
still-recording one gets `no-store`), and the RFC-7807 problem-details that turn the day-13
error hierarchy into a status code *and a distinct user action*. It binds `127.0.0.1` by
default -- a threat-model decision, not a convenience: a recording is program memory, i.e.
secrets, and a debugger that bound `0.0.0.0` would publish them to the LAN.

What it must never know
-----------------------
The bytes of the format, the shape of a delta, the id-space of the intern tables. It asks
`query`/`reconstruct` for answers and maps them to DTOs; it resolves ids to text at that
boundary and never before. The arrow points down; nothing above `server` exists yet.

Scope, written down before the UI tempts otherwise (ADR-0011): **Phase 5 ships a scrubber,
not an IDE.** No editing, no gutter-breakpoint UI, no multi-session diff, no extension.

Design: [ADR-0010](../../../docs/adr/0010-api-contract.md) (contract),
[ADR-0011](../../../docs/adr/0011-phase5-scope.md) (non-goals). The FastAPI server lands day 33
in `app.py` (factory + lifespan), `deps.py` (resources), `routes/`, and `errors.py`.

The FastAPI + uvicorn dependencies live behind the optional `[ui]` extra, so a user who only
records still installs nothing web -- the same import-cleanliness rule the recorder keeps.

The app factory lives in `chronotrace.server.app` (`create_app`, `ServerConfig`); it is
imported from there lazily -- by the CLI's `serve`, or a test -- so importing this package
for its `dto` never requires FastAPI to be installed.
"""

from chronotrace.server import dto

__all__ = ["dto"]
