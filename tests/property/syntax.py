"""The tree the grammar builds, and the scope it builds it in.

Two node types, not twelve. `Line` is a simple statement and `Block` is a header plus an
indented body plus optional trailing clauses (`else:`, `except:`, `finally:`). That is
the entire indentation-sensitive part of Python's syntax -- the only thing a text
generator actually gets wrong -- so the renderer cannot emit bad indentation and more node
types would buy nothing. Expressions and block headers are strings, built by the grammar
in `program_gen.py` from names `Env` says are in scope.

`ast` + `ast.unparse` was the other option and was rejected on volume: constructing real
`ast` nodes (every `arguments` field, version-dependent shapes) is far more code than the
twelve-line renderer it would save.

`Env` is what makes generation context-sensitive rather than context-free. A context-free
grammar cannot express "`nonlocal x` requires x bound in an enclosing function scope", so
the environment carries that knowledge and the productions read it.

Split from `program_gen.py` only because that file passed 400 lines, but the seam is a
real one: this is the substrate, that is the grammar over it.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace


@dataclass(frozen=True, slots=True)
class Line:
    """A simple statement -- one rendered line at the current indentation."""

    text: str


@dataclass(frozen=True, slots=True)
class Block:
    """A compound statement: a header, an indented body, and any trailing clauses.

    `clauses` carries `else:`, `except ...:` and `finally:` -- headers that belong to the
    same statement but sit at the parent's indentation.
    """

    header: str
    body: tuple[Stmt, ...]
    clauses: tuple[tuple[str, tuple[Stmt, ...]], ...] = ()


type Stmt = Line | Block


def render(body: tuple[Stmt, ...], indent: int = 0) -> list[str]:
    """Render a statement tree to source lines. Cannot produce bad indentation.

    An empty body renders as `pass`, because a `Block` with nothing in it is the one way
    this tree could still produce a syntax error.
    """
    pad = "    " * indent
    out: list[str] = []
    if not body:
        return [f"{pad}pass"]
    for stmt in body:
        if isinstance(stmt, Line):
            out.append(pad + stmt.text)
            continue
        out.append(pad + stmt.header)
        out.extend(render(stmt.body, indent + 1))
        for header, clause_body in stmt.clauses:
            out.append(pad + header)
            out.extend(render(clause_body, indent + 1))
    return out


@dataclass(frozen=True, slots=True)
class Env:
    """What is in scope where a statement is being generated.

    Attributes:
        ints: names currently holding an int.
        lists: names currently holding a list of ints.
        funcs: names callable with one int argument, **returning an int**.
        gens: generator functions. Kept apart from `funcs` because calling one yields a
            generator object, not an int -- letting them share a bucket would put a
            generator where the grammar promised an int and produce a `TypeError` the
            grammar never meant to generate.
        enclosing: names bound in an enclosing *function* scope -- the legal `nonlocal`
            targets, and the reason this generator is context-sensitive rather than
            context-free.
        deletable: names bound in the **current block**, and the only legal `del` targets.
            Narrower than `ints` on purpose: a nested block cannot delete a name the
            enclosing block still believes is bound, because the enclosing block's
            environment is not threaded back out of an `if` or a `for` -- and a reference
            after that branch would be an `UnboundLocalError` the grammar never meant to
            generate.
        in_function: whether `return` and `yield` are legal here.
        supply: the shared unique-name counter, so no two scopes collide by accident and
            shadowing only happens where a production means it.
    """

    ints: tuple[str, ...] = ()
    lists: tuple[str, ...] = ()
    funcs: tuple[str, ...] = ()
    gens: tuple[str, ...] = ()
    enclosing: tuple[str, ...] = ()
    deletable: tuple[str, ...] = ()
    in_function: bool = False
    supply: itertools.count[int] = field(default_factory=lambda: itertools.count())

    def fresh(self, prefix: str) -> str:
        return f"{prefix}{next(self.supply)}"

    def nested(self) -> Env:
        """The environment for a nested block: everything visible, nothing deletable.

        Bindings and deletions are not symmetric, which is the subtlety. A name *bound*
        inside an `if` is deliberately not propagated out, so nothing after the branch
        refers to it. A name *deleted* inside an `if` stays deleted after it, at runtime,
        if the branch ran -- so a nested block that could delete an outer name would make
        every later reference an `UnboundLocalError`. Found by the campaign.
        """
        return replace(self, deletable=())

    def names(self) -> tuple[str, ...]:
        return self.ints + self.lists
