"""Turn any `seq` into the program state at that instant. The product, as a function.

Everything the user touches -- the scrubber, backward stepping, the variable panel, the
call stack -- is `reconstruct(seq)`. The design (algorithm, complexity proof, backward-
step decision, cache) is [ADR-0006](../../../docs/adr/0006-reconstruction.md); the
implementation lands day 20. Today ships the vocabulary: `ProgramState` and the
`Reconstructor` protocol.

What this layer OWNS
--------------------
The reconstruction algorithm: nearest keyframe + a bounded replay of deltas (forward)
and their inversion (backward), plus the locality cache that makes dragging a playhead
feel instant. It produces `ProgramState`, the storage-agnostic DTO every layer above
serves.

What it must NEVER know
-----------------------
The file format's *bytes* -- framing, CRCs, compression, mmap, block layout. That is
`store`, and this layer reads through `store`'s typed surface only (`ChronoReader`:
keyframes, deltas, events, value refs), never a raw block. Nor may it import `index`,
`query`, `server` (the dependency arrow points down). It resolves nothing: a
`ProgramState` carries `name_id`/`code_id`/`value_ref` *ids*, and turning those into
names, source and values is a higher layer's job -- which is why `server` can serialise
a `ProgramState` without importing a single storage type.

The instant, shared with keyframes
----------------------------------
`ProgramState.seq` is the state **after** event `seq` has executed -- the exact word
`keyframe.py` uses. If that word ever differs by one, an off-by-one haunts the scrubber
for a week; it is asserted identical in the tests.
"""
