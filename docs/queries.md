# Querying a recording

Reconstruction answers *"what was the state at instant S?"*. Queries answer the questions
you actually ask while debugging — *when* did this change, *where* did this line run — and
the answer to every one of them is a set of **instants you can jump to**. That is the whole
idea: a query result is not text, it is a list of `seq` numbers (ChronoTrace's universal
address), each of which you can land on and inspect. A query that returns text is a `grep`;
a query that returns instants is a debugger.

Each query is a **typed callable** with a typed result — no query language to learn, and
none to keep stable before we know which queries matter (see [No DSL](#no-dsl-yet), below).

## From the command line

```bash
chronotrace query --list                          # what queries exist
chronotrace query run.chrono --var-writes total   # every write to `total`
chronotrace query run.chrono --line-hits app.py:42
```

The index is built on demand the first time you query a recording, so a bare `.chrono`
just works — it waits once rather than failing with "run `index` first". Each result line
leads with its `[seq]`, the address the UI will make clickable:

```
[22]  simple.py  main  = 0
[69]  simple.py  main  = 4
```

| flag | meaning |
|---|---|
| `--var-writes NAME` | every write to variable `NAME` |
| `--line-hits FILE:LINE` | every instant `FILE:LINE` executed |
| `--frame ID` | scope `--var-writes` to one invocation (see recursion, below) |
| `--before SEQ` | only writes strictly before instant `SEQ` |
| `--after SEQ` | resume paging after this instant (the cursor from a previous page) |
| `--limit N` | rows per page (default 100) |
| `--list` | list the available queries and exit |

## The queries

### `var-writes` — every write to a variable

`VarWritesQuery(name, frame_id=None, before_seq=None)`

Returns each instant the variable was written, oldest first, with the value written
(`= 4`) and the function it happened in. Built on the `var_writes` index, clustered on
`(name_id, seq)`.

- **Arguments.** `name` as you typed it (resolved to the recording's interned id; an
  unrecorded name is a typo, not an empty result — it raises `UnknownName`). `frame_id`
  restricts to a single invocation. `before_seq` bounds the search to writes before an
  instant — *"who last set this, before it went wrong?"*.
- **Recursion.** `total` in one call and `total` in another are different variables sharing
  a name. Without `frame_id` the query answers across all invocations; with it, one. This
  is why every write carries the `frame_id` the recorder attributed it to.
- **Change detection.** A `VAR_WRITE` is recorded only when the value actually changes
  (day-8 content-addressed dedup), so `x = 0; x = 0` is one write, not two. The query
  reflects the recording faithfully — it answers what happened, not what the source text
  says should have.
- **Complexity.** O(log n + page) per page — a range scan on the clustered key, already in
  `seq` order, no sort. Independent of how many times the variable was written.

```bash
chronotrace query run.chrono --var-writes total --before 500
```

### `line-hits` — every execution of a line

`LineHitsQuery(file, lineno)`

Returns each instant the line executed, oldest first — a **retroactive breakpoint**: the
program already finished, so the time-travel form of "break on line 42" is the list of
places you could have stopped. Built on the `line_hits` index, clustered on
`(file_id, lineno, seq)`.

- **Arguments.** `file` may be a full path or a bare name (`app.py`); a bare name is fine
  unless two recorded files share it, in which case it is rejected with the candidates
  rather than guessed. `lineno` is 1-based.
- **Three situations, two we can tell apart.** A file not in the recording raises
  `UnknownFile` (a typo). A known file whose line has no hits is an *empty* result — and
  that covers both "the line never ran" and "the line is blank / a comment / past the end
  of the file", because distinguishing those needs the *source*, which the index does not
  store. The source pane (day 35) will separate them when source is at hand.
- **Complexity.** O(log n + page), a clustered range scan in `seq` order — so "next hit
  after S" and "previous hit before S" are both free (day 30 builds stepping on them).

```bash
chronotrace query run.chrono --line-hits app.py:42
```

## Pagination

No query returns an unbounded list — "every write to `i`" in a hot loop is ten million
rows, and a query engine that can exhaust the UI's memory is not done. Results come one
`--limit`-sized page at a time. When more remain, the CLI prints the cursor to resume with:

```
chronotrace: more results -- rerun with --after 29
```

The cursor is a `seq`, not an `OFFSET`. That is a correctness choice as much as a speed
one: `OFFSET n` re-reads and discards the first `n` rows on every page (paging to the end
is O(rows²)), while a `seq` cursor is an indexed seek that costs the same for page one and
page ten thousand — and because `seq` is unique and monotonic, it can neither skip a row
nor return one twice.

## Latency and partial results

The query engine ships a **latency contract**: p95 under 50 ms on a ten-million-event
recording, asserted by a test that also checks the query plan (a budget met by accident of
a full scan does not pass). The clustered indexes keep a page query at O(log n + page).

A query over a **crash-truncated** recording answers only for the events that survived, and
says so — every result carries a `partial` flag and the CLI prints a `PARTIAL` warning.
Silently returning fewer instants than exist is the one thing a debugger must not do.

## No DSL (yet)

There is deliberately no query *language* — no `"writes to total where seq < 500"`. A DSL
needs a lexer, a grammar, a parser, error messages good enough to debug against,
documentation, and — the moment anyone scripts it — a stability promise that outlives every
internal refactor, all built before we know which queries people actually run. A typed
callable costs none of that: the type checker is the parser, the IDE is the documentation,
and a signature change is caught at the call site instead of at a user's runtime.

The restraint has an explicit trigger. A DSL is revisited when a real user is composing
three or more of these queries by hand, repeatedly, because the typed API cannot express
what they mean — tracked as [issue #13](https://github.com/dharmppp21/ChronoTrace/issues/13).
Until then it would be generality with no demand.
