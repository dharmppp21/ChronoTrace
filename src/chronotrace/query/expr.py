"""A restricted expression evaluator over captured values -- and why it is NEVER `eval`.

The threat, concretely. A conditional breakpoint's condition is **user-supplied source**:
`i > 100`. That string arrives in a shared session, from a URL, and (day 34) inside an HTTP
request. `eval("i > 100", ...)` on any of those is arbitrary code execution in the user's
own process -- `eval("__import__('os').system('rm -rf ~')", ...)` is the same code path. So
this module parses the condition with `ast`, **whitelists** the node types it will execute,
and walks the tree itself. ~150 lines of walker beats `eval` (total control of the grammar,
no sandbox-escape surface, clear errors) and beats a dependency (nothing to audit but this).

The elegant part, worth saying out loud: **the values are captured *data*, not live
objects.** A variable here is a nested dict/list/atom from day-7 capture, so an attribute
access has nothing dangerous to reach -- there is no `__class__`, no `__globals__`, no
callable, because there is no live object. `().__class__.__bases__` cannot walk to `object`
and down to `os`, because `()` never became a live tuple. That security property falls out
of the day-7 design; the whitelist is a second wall in front of a domain that is already
safe.

Grammar (the whitelist): comparisons (`< <= > >= == != in`, chained), boolean ops
(`and or not`), unary `+ -`, arithmetic (`+ - * / // % **`), number/string/`True`/`False`/
`None` literals, list/tuple/set literals, name lookups, subscript, and attribute access on
captured objects. **Rejected at parse**, every one: calls, lambdas, comprehensions, the
walrus, f-strings, starred, slices, and any name or attribute beginning with an underscore.

Three-valued, because a debugger must not lie
---------------------------------------------
`evaluate` returns `True`, `False`, or **`None` (unknown)**. Unknown when the condition
needed a value the recording only has a summary of -- a truncated string or list, a redacted
secret, a value dropped at the capture budget or depth limit -- or a name/attribute/index
that could not be resolved. A conditional breakpoint that silently answered `False` for a
value it could not see would be claiming the program did not match when it does not know:
that is the one thing a debugger cannot do.
"""

from __future__ import annotations

import ast
import operator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from chronotrace.recorder.capture import CapturedValue


class ConditionError(Exception):
    """The condition is not valid: a syntax error, or a construct outside the grammar.

    Carries the offending position so the CLI can point at it. Raised at *compile* time, so
    a forbidden construct never reaches evaluation -- the whitelist is a parse gate.
    """


UNKNOWN = object()
"""The third truth value: the condition touched something the recording only summarised, so
its answer is genuinely not known. Distinct from `False`, and never collapsed into it."""

_MARKERS = frozenset({"budget", "depth", "cycle", "obj", "redacted"})
"""Captured tags that are not a value we can reason about: a walk that stopped (budget,
depth), a back-reference (cycle), an opaque object, a withheld secret. Touching one is
`UNKNOWN` -- we do not have the thing the condition asked about."""

_COMPARE = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_BINOP = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.BinOp,
    ast.Compare,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Subscript,
    ast.Attribute,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.In,
    ast.NotIn,
    *_COMPARE,
    *_BINOP,
)
"""Every AST node type the walker will execute. A whitelist, never a blacklist: a node type
not in here is rejected, so a construct nobody thought to forbid is forbidden by default --
which is the only safe direction for a security gate."""


@dataclass(frozen=True, slots=True)
class Condition:
    """A compiled, validated condition -- parse once, evaluate against many instants.

    `names` are the free variables it reads, which the breakpoint query uses for predicate
    pushdown: a hit where none of these changed cannot flip the condition's value.
    """

    source: str
    tree: ast.Expression
    names: frozenset[str]

    def evaluate(self, bindings: Mapping[str, CapturedValue]) -> bool | None:
        """`True` / `False` / `None` (unknown) for these bindings. Never raises, never lies."""
        value = _eval(self.tree.body, bindings)
        return None if value is UNKNOWN else bool(value)


def compile_condition(source: str) -> Condition:
    """Parse and whitelist-validate a condition. Raises `ConditionError` on anything unsafe."""
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ConditionError(
            f"invalid condition {source!r}: {exc.msg} (column {exc.offset})"
        ) from exc
    _validate(tree)
    return Condition(source=source, tree=tree, names=_free_names(tree))


def _validate(tree: ast.Expression) -> None:
    """Reject any node type outside the grammar, or an underscore name/attribute (dunders)."""
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ConditionError(f"{type(node).__name__} is not allowed in a condition")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ConditionError(f"attribute {node.attr!r} is not allowed (underscore access)")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ConditionError(f"name {node.id!r} is not allowed (dunder)")


def _free_names(tree: ast.Expression) -> frozenset[str]:
    return frozenset(n.id for n in ast.walk(tree) if isinstance(n, ast.Name))


def to_python(value: CapturedValue) -> Any:
    """A captured value as a plain Python value for comparison, or `UNKNOWN` if summarised.

    Public because the watch query filters (`--changed-to`) compare a recorded value against
    a literal, and must make the same honest call: a truncated value is `UNKNOWN`, never
    silently unequal.
    """
    return _scalar(value)


def _eval(node: ast.expr, env: Mapping[str, CapturedValue]) -> Any:
    """One node to a value, a captured container, or `UNKNOWN`. The dispatch core."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return env.get(node.id, UNKNOWN)  # a name not in scope is unknown, never False
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return _literal_collection(node, env)
    if isinstance(node, ast.BoolOp):
        return _eval_boolop(node, env)
    if isinstance(node, ast.UnaryOp):
        return _eval_unaryop(node, env)
    if isinstance(node, ast.BinOp):
        return _eval_binop(node, env)
    if isinstance(node, ast.Compare):
        return _eval_compare(node, env)
    if isinstance(node, ast.Subscript):
        return _index(_eval(node.value, env), _scalar(_eval(node.slice, env)))
    if isinstance(node, ast.Attribute):
        return _attr(_eval(node.value, env), node.attr)
    return UNKNOWN  # unreachable: _validate rejected every other node type


def _literal_collection(
    node: ast.List | ast.Tuple | ast.Set, env: Mapping[str, CapturedValue]
) -> Any:
    items = [_scalar(_eval(e, env)) for e in node.elts]
    if any(i is UNKNOWN for i in items):
        return UNKNOWN
    return set(items) if isinstance(node, ast.Set) else items


def _eval_boolop(node: ast.BoolOp, env: Mapping[str, CapturedValue]) -> Any:
    """Kleene `and`/`or`: `False` dominates `and`, `True` dominates `or`, else UNKNOWN wins."""
    truths = [_truth(_eval(v, env)) for v in node.values]
    if isinstance(node.op, ast.And):
        if any(t is False for t in truths):
            return False
        return UNKNOWN if UNKNOWN in truths else True
    if any(t is True for t in truths):
        return True
    return UNKNOWN if UNKNOWN in truths else False


def _eval_unaryop(node: ast.UnaryOp, env: Mapping[str, CapturedValue]) -> Any:
    operand = _eval(node.operand, env)
    if isinstance(node.op, ast.Not):
        truth = _truth(operand)
        return UNKNOWN if truth is UNKNOWN else not truth
    value = _scalar(operand)
    if value is UNKNOWN or not isinstance(value, (int, float, complex)):
        return UNKNOWN
    return -value if isinstance(node.op, ast.USub) else +value


def _eval_binop(node: ast.BinOp, env: Mapping[str, CapturedValue]) -> Any:
    left, right = _scalar(_eval(node.left, env)), _scalar(_eval(node.right, env))
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    try:
        return _BINOP[type(node.op)](left, right)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return UNKNOWN  # e.g. "s" - 1, or a divide by zero: not a match, but not a crash


def _eval_compare(node: ast.Compare, env: Mapping[str, CapturedValue]) -> Any:
    """Chained comparisons, three-valued. Membership walks a captured container honestly."""
    left = _eval(node.left, env)
    for op, right_node in zip(node.ops, node.comparators, strict=True):
        right = _eval(right_node, env)
        if isinstance(op, (ast.In, ast.NotIn)):
            result = _membership(_scalar(left), right)
            result = result if isinstance(op, ast.In) else _negate(result)
        else:
            result = _ordered_compare(op, _scalar(left), _scalar(right))
        if result is UNKNOWN or result is False:
            return result  # chained comparison short-circuits on the first non-True link
        left = right
    return True


def _ordered_compare(op: ast.cmpop, left: Any, right: Any) -> Any:
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    try:
        return _COMPARE[type(op)](left, right)
    except TypeError:
        return UNKNOWN  # comparing incomparable types: unknown, never a confident False


def _membership(needle: Any, container: Any) -> Any:
    """`needle in container`, honest about truncation.

    Found -> True; not found in a truncated container -> UNKNOWN (it may be in the part we
    did not capture).
    """
    if needle is UNKNOWN:
        return UNKNOWN
    if isinstance(container, (list, tuple, set)):  # a literal collection from the condition
        return needle in container
    if not _is_captured(container):
        return UNKNOWN
    items = _visible_items(container)
    if items is None:
        return UNKNOWN
    if any(_scalar(item) == needle for item in items):
        return True
    return UNKNOWN if container.get("truncated") else False


def _negate(result: Any) -> Any:
    return UNKNOWN if result is UNKNOWN else not result


def _truth(value: Any) -> Any:
    """A value's truthiness as `True`/`False`/`UNKNOWN` -- for boolean ops and the result."""
    scalar = _scalar(value)
    return UNKNOWN if scalar is UNKNOWN else bool(scalar)


def _scalar(value: Any) -> Any:
    """Reduce a value to something comparable: a primitive, or `UNKNOWN`.

    Captured containers are materialised whole, becoming UNKNOWN if any part was summarised.
    """
    if value is UNKNOWN:
        return UNKNOWN
    if _is_captured(value):
        return _materialize(value)
    return value


def _materialize(captured: dict[str, Any]) -> Any:
    """A captured container to a concrete Python value, or `UNKNOWN` if it was summarised."""
    tag = captured.get("$")
    if tag in _MARKERS or captured.get("truncated"):
        return UNKNOWN
    if tag == "bytes":
        return bytes.fromhex(captured["v"])
    items = captured.get("items")
    if items is None:
        return UNKNOWN
    if tag == "dict":
        pairs = [(_scalar(k), _scalar(v)) for k, v in items]
        return UNKNOWN if _any_unknown(pairs) else dict(pairs)
    values = [_scalar(v) for v in items]
    if any(v is UNKNOWN for v in values):
        return UNKNOWN
    return (
        tuple(values) if tag == "tuple" else set(values) if tag in {"set", "frozenset"} else values
    )


def _index(container: Any, key: Any) -> Any:
    """`container[key]` over captured data: sequence by position, dict by key.

    UNKNOWN if the element was not captured -- out of range in a truncated container, or a
    missing key.
    """
    if not _is_captured(container) or key is UNKNOWN:
        return UNKNOWN
    items = _visible_items(container)
    if items is None:
        return UNKNOWN
    if container.get("$") == "dict":
        for k, v in items:
            if _scalar(k) == key:
                return v
        return UNKNOWN
    if isinstance(key, int) and -len(items) <= key < len(items):
        return items[key]
    return UNKNOWN  # out of the captured range: may exist in a truncated tail


def _attr(obj: Any, name: str) -> Any:
    """`obj.name` over a captured object's recorded state. UNKNOWN if unrecorded or opaque."""
    if not _is_captured(obj) or obj.get("$") != "obj":
        return UNKNOWN
    attrs = obj.get("attrs")
    if attrs is None or name not in attrs:
        return UNKNOWN
    return attrs[name]


def _visible_items(container: dict[str, Any]) -> list[Any] | None:
    items = container.get("items")
    return items if isinstance(items, list) else None


def _is_captured(value: Any) -> bool:
    return isinstance(value, dict) and "$" in value


def _any_unknown(pairs: list[tuple[Any, Any]]) -> bool:
    return any(k is UNKNOWN or v is UNKNOWN for k, v in pairs)
