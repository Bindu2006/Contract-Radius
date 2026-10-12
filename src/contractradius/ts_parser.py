"""Find HTTP calls in TypeScript/TSX and trace which response fields the code reads.

What is recognised (deliberately narrow; see README "Limitations"):

* ``fetch(url, {method, body})`` and ``<client>.<get|post|put|patch|delete>(url, data)``
  where ``<client>`` is one of the configured client names (axios, apiClient, ...).
* The result consumed via: ``const x = await call``, ``const {data: {a}} = await call``,
  ``(await call).data.a``, ``call.then(r => ...)``, ``await res.json()`` for fetch.
* Field reads via member chains (``x.a.b``), string subscripts, ``x[0]`` (array item),
  array callbacks (``x.items.map(i => i.id)``), destructuring, and simple aliases.

Everything is scoped to the enclosing function so same-named variables in different
functions don't contaminate each other. Calls whose URL is not a static string are
reported as "unanalyzed" instead of guessed at.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_typescript as tsts
from tree_sitter import Language, Node, Parser

from contractradius.exceptions import FrontendError
from contractradius.models import CallSite, FieldUsage, UnanalyzedCall

HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}
BODY_METHODS = {"POST", "PUT", "PATCH"}
SKIP_DIRS = {"node_modules", "dist", "build", ".next", "coverage", ".git", "out", ".turbo"}
FRONTEND_SUFFIXES = {".ts", ".tsx"}

_CALLBACK_METHODS = {"map", "forEach", "filter", "find", "findIndex", "some", "every", "flatMap"}
_ARRAY_METHODS = _CALLBACK_METHODS | {
    "length", "slice", "concat", "join", "includes", "indexOf", "at", "sort", "reverse",
    "reduce", "entries", "keys", "values",
}
_FUNCTION_TYPES = {
    "arrow_function", "function_expression", "function", "function_declaration",
    "method_definition", "generator_function",
}
_TRANSPARENT = {"await_expression", "parenthesized_expression", "as_expression",
                "non_null_expression"}

# Confidence assigned to the *evidence of use*, before path-matching adjustments.
CONF_EXPLICIT = 0.9  # code explicitly reads this field
CONF_BARE = 0.4  # an object containing the field is passed around whole
CONF_WHOLE_BODY = 0.3  # the whole response body is used; fields unknown

_PARSERS: dict[str, Parser] = {}


def _parser(suffix: str) -> Parser:
    key = "tsx" if suffix == ".tsx" else "ts"
    if key not in _PARSERS:
        lang = tsts.language_tsx() if key == "tsx" else tsts.language_typescript()
        _PARSERS[key] = Parser(Language(lang))
    return _PARSERS[key]


# ---------------------------------------------------------------- tiny AST helpers


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8") if node is not None and node.text is not None else ""


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _line(node: Node) -> int:
    return int(node.start_point[0]) + 1


def _args(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    return [c for c in args.named_children if c.type != "comment"] if args else []


def _scope_of(node: Node) -> Node:
    current = node.parent
    while current is not None:
        if current.type in _FUNCTION_TYPES or current.type == "program":
            return current
        current = current.parent
    return node


def _string_value(node: Node) -> str:
    return _text(node)[1:-1]


def _key_text(node: Node | None) -> str:
    text = _text(node)
    return text[1:-1] if node is not None and node.type == "string" else text


def _first_param_pattern(fn: Node) -> Node | None:
    single = fn.child_by_field_name("parameter")
    if single is not None:
        return single
    params = fn.child_by_field_name("parameters")
    if params is None:
        return None
    for child in params.named_children:
        if child.type in ("required_parameter", "optional_parameter"):
            return child.child_by_field_name("pattern")
        if child.type == "identifier":
            return child
    return None


def _is_declaration(node: Node) -> bool:
    """True if this identifier *declares* a name rather than reading it."""
    parent = node.parent
    if parent is None:
        return False
    checks = {
        "variable_declarator": "name",
        "required_parameter": "pattern",
        "optional_parameter": "pattern",
        "pair_pattern": "value",
        "arrow_function": "parameter",
        "assignment_pattern": "left",
    }
    field_name = checks.get(parent.type)
    return field_name is not None and parent.child_by_field_name(field_name) == node


def _declarator_for(node: Node) -> Node | None:
    """The `const x = <node>` declarator whose value is this expression, if any."""
    current = node
    while (
        current.parent is not None
        and current.parent.type in ("as_expression", "non_null_expression")
        and current.parent.named_children[0] == current
    ):
        current = current.parent
    parent = current.parent
    if parent is not None and parent.type == "variable_declarator":
        if parent.child_by_field_name("value") == current:
            return parent
    return None


def path_str(elems: tuple[str, ...] | list[str]) -> str:
    """('items', '[]', 'id') -> 'items[].id'; same key format the spec diff produces."""
    out = ""
    for elem in elems:
        out += "[]" if elem == "[]" else (f".{elem}" if out else elem)
    return out


# ---------------------------------------------------------------- URL / request extraction


def _static_string(node: Node) -> str | None:
    """String value of a literal/template/concatenation; unknown parts become '{}'."""
    kind = node.type
    if kind == "string":
        return _string_value(node)
    if kind == "template_string":
        parts = []
        for child in node.children:
            if child.type == "template_substitution":
                parts.append("{}")
            elif child.type in ("string_fragment", "escape_sequence"):
                parts.append(_text(child))
        return "".join(parts)
    if kind == "binary_expression" and _text(node.child_by_field_name("operator")) == "+":
        left = _static_string(node.child_by_field_name("left"))  # type: ignore[arg-type]
        right = _static_string(node.child_by_field_name("right"))  # type: ignore[arg-type]
        if left is None and right is None:
            return None
        return (left if left is not None else "{}") + (right if right is not None else "{}")
    if kind in ("parenthesized_expression", "as_expression") and node.named_children:
        return _static_string(node.named_children[0])
    return None


def normalize_call_path(raw: str) -> str | None:
    path = re.sub(r"^https?://[^/]+", "", raw)
    path = path.split("?")[0].split("#")[0]
    return path if "/" in path else None


def _object_keys(node: Node | None) -> set[str] | None:
    """Top-level keys of an object literal; None if not statically knowable."""
    if node is None:
        return None
    while node.type in ("parenthesized_expression", "as_expression") and node.named_children:
        node = node.named_children[0]
    if node.type != "object":
        return None
    keys: set[str] = set()
    for child in node.named_children:
        if child.type == "pair":
            keys.add(_key_text(child.child_by_field_name("key")))
        elif child.type == "shorthand_property_identifier":
            keys.add(_text(child))
        elif child.type == "spread_element":
            return None
    return keys


def _object_pair(obj: Node | None, key: str) -> Node | None:
    if obj is None or obj.type != "object":
        return None
    for child in obj.named_children:
        if child.type == "pair" and _key_text(child.child_by_field_name("key")) == key:
            return child.child_by_field_name("value")
    return None


# ---------------------------------------------------------------- response-use tracing


def _normalize(kind: str, path: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
    """Convert a path relative to a binding's root into (kind, body-relative path).

    kinds: 'response' (axios response object; body lives under .data),
           'fetch_response' (only .json() matters), 'body' (parsed JSON body).
    """
    if kind == "response":
        if not path:
            return "response", ()
        return ("body", path[1:]) if path[0] == "data" else None
    if kind == "fetch_response":
        return ("fetch_response", ()) if not path else None
    return "body", path


def _climb(start: Node) -> tuple[list[str], Node, str | None, Node | None]:
    """Follow a member/subscript chain upward from `start`.

    Returns (property names, last node of the chain, terminating array method, that
    method's member_expression). Stops *before* array methods like .map/.length.
    """
    elems: list[str] = []
    current = start
    while current.parent is not None:
        parent = current.parent
        if parent.type in ("non_null_expression", "parenthesized_expression") or (
            parent.type == "as_expression" and parent.named_children[0] == current
        ):
            current = parent
            continue
        if parent.type == "member_expression" and parent.child_by_field_name("object") == current:
            prop = _text(parent.child_by_field_name("property"))
            if prop in _ARRAY_METHODS:
                return elems, current, prop, parent
            elems.append(prop)
            current = parent
            continue
        if parent.type == "subscript_expression" and parent.child_by_field_name("object") == current:
            index = parent.child_by_field_name("index")
            elems.append(_string_value(index) if index is not None and index.type == "string"
                         else "[]")
            current = parent
            continue
        break
    return elems, current, None, None


class _Tracer:
    """Collects FieldUsage records for the value produced by one HTTP call."""

    def __init__(self) -> None:
        self.usages: list[FieldUsage] = []
        self._seen_usages: set[tuple[str, int, float, bool]] = set()
        self._seen_scans: set[tuple[int, str, str, tuple[str, ...]]] = set()

    def add(self, field_path: str, line: int, confidence: float, precise: bool = False) -> None:
        key = (field_path, line, confidence, precise)
        if key not in self._seen_usages:
            self._seen_usages.add(key)
            self.usages.append(FieldUsage(field_path, line, confidence, precise))

    # -- entry point: `value_node` evaluates to the response (or body) of the call
    def trace(self, value_node: Node, kind: str, depth: int = 0) -> None:
        if depth > 10:
            return
        current = value_node
        while (
            current.parent is not None
            and current.parent.type in _TRANSPARENT
            and (current.parent.type != "as_expression" or current.parent.named_children[0] == current)
        ):
            current = current.parent

        declarator = _declarator_for(current)
        if declarator is not None:
            self.bind(declarator.child_by_field_name("name"), kind, (), _scope_of(declarator), depth)
            return

        parent = current.parent
        if parent is None or parent.type != "member_expression":
            return  # returned / passed along: cannot follow
        if parent.child_by_field_name("object") != current:
            return
        prop = _text(parent.child_by_field_name("property"))
        grand = parent.parent
        is_call = (
            grand is not None
            and grand.type == "call_expression"
            and grand.child_by_field_name("function") == parent
        )
        if prop == "then" and is_call and grand is not None:
            self._then(grand, kind, depth)
        elif prop in ("catch", "finally") and is_call and grand is not None:
            self.trace(grand, kind, depth + 1)
        elif prop == "json" and kind == "fetch_response" and is_call and grand is not None:
            self.trace(grand, "body", depth + 1)
        else:
            self.emit(current, kind, (), depth + 1)

    def _then(self, call: Node, kind: str, depth: int) -> None:
        args = _args(call)
        callback = args[0] if args else None
        if callback is None or callback.type not in _FUNCTION_TYPES:
            return
        pattern = _first_param_pattern(callback)
        self.bind(pattern, kind, (), callback, depth + 1)

        # `.then(r => r.json())` / `.then(r => r.data)` resolve to the body for the next .then
        body = callback.child_by_field_name("body")
        if body is None or body.type == "statement_block" or pattern is None:
            return
        name = _text(pattern) if pattern.type == "identifier" else None
        if kind == "fetch_response" and body.type == "call_expression":
            fn = body.child_by_field_name("function")
            if (fn is not None and fn.type == "member_expression"
                    and _text(fn.child_by_field_name("object")) == name
                    and _text(fn.child_by_field_name("property")) == "json"):
                self.trace(call, "body", depth + 1)
        elif kind == "response" and body.type == "member_expression":
            if (_text(body.child_by_field_name("object")) == name
                    and _text(body.child_by_field_name("property")) == "data"):
                self.trace(call, "body", depth + 1)

    # -- binding a pattern (identifier or destructuring) to a traced value
    def bind(self, pattern: Node | None, kind: str, path: tuple[str, ...], scope: Node,
             depth: int) -> None:
        if pattern is None or depth > 10:
            return
        if pattern.type == "identifier":
            norm = _normalize(kind, path)
            if norm is not None:
                self.scan(scope, _text(pattern), norm[0], norm[1], pattern, depth + 1)
        elif pattern.type == "object_pattern":
            for child in pattern.named_children:
                if child.type == "shorthand_property_identifier_pattern":
                    self._leaf(child, kind, (*path, _text(child)), scope, depth)
                elif child.type == "object_assignment_pattern":
                    left = child.child_by_field_name("left")
                    if left is not None:
                        self._leaf(left, kind, (*path, _text(left)), scope, depth)
                elif child.type == "pair_pattern":
                    key = _key_text(child.child_by_field_name("key"))
                    value = child.child_by_field_name("value")
                    if value is not None and value.type == "assignment_pattern":
                        value = value.child_by_field_name("left")
                    if value is None:
                        continue
                    if value.type == "identifier":
                        self._leaf(value, kind, (*path, key), scope, depth)
                    elif value.type == "object_pattern":
                        self.bind(value, kind, (*path, key), scope, depth + 1)

    def _leaf(self, name_node: Node, kind: str, path: tuple[str, ...], scope: Node,
              depth: int) -> None:
        norm = _normalize(kind, path)
        if norm is None:
            return
        new_kind, body_path = norm
        if new_kind == "body" and body_path:
            self.add(path_str(body_path), _line(name_node), CONF_EXPLICIT)
        self.scan(scope, _text(name_node), new_kind, body_path, name_node, depth + 1)

    # -- find reads of a bound name inside its scope
    def scan(self, scope: Node, name: str, kind: str, prefix: tuple[str, ...], decl: Node,
             depth: int) -> None:
        key = (scope.id, name, kind, prefix)
        if key in self._seen_scans or depth > 10:
            return
        self._seen_scans.add(key)

        for node in _walk(scope):
            if _text(node) != name:
                continue
            if node.type == "identifier":
                if node == decl or _is_declaration(node):
                    continue
                if kind == "fetch_response":
                    self._scan_fetch(node, depth)
                else:
                    self.emit(node, kind, prefix, depth + 1)
            elif node.type == "shorthand_property_identifier" and kind == "body":
                # `{ data }` in an object literal: passes the whole thing along
                if prefix:
                    self.add(path_str(prefix), _line(node), CONF_BARE)
                else:
                    self.add("*", _line(node), CONF_WHOLE_BODY)

    def _scan_fetch(self, node: Node, depth: int) -> None:
        parent = node.parent
        if parent is None or parent.type != "member_expression":
            return
        if parent.child_by_field_name("object") != node:
            return
        if _text(parent.child_by_field_name("property")) != "json":
            return
        grand = parent.parent
        if grand is not None and grand.type == "call_expression":
            self.trace(grand, "body", depth + 1)

    # -- record what one use of a bound value does
    def emit(self, start: Node, kind: str, prefix: tuple[str, ...], depth: int) -> None:
        line = _line(start)
        elems, end, array_method, _member = _climb(start)
        full = (*prefix, *elems)
        norm = _normalize(kind, full)
        if norm is None:
            return
        new_kind, body_path = norm

        if array_method is not None:
            if new_kind != "body":
                return
            traced = False
            if array_method in _CALLBACK_METHODS and _member is not None:
                traced = self._bind_callback(_member, (*body_path, "[]"), depth)
            if body_path:
                self.add(path_str(body_path), line, CONF_EXPLICIT,
                         precise=traced or array_method == "length")
            return

        declarator = _declarator_for(end)
        if declarator is not None:
            if new_kind == "body" and body_path and elems:
                self.add(path_str(body_path), line, CONF_EXPLICIT)
            self.bind(declarator.child_by_field_name("name"), kind, full, _scope_of(declarator),
                      depth)
            return

        if new_kind != "body":
            return
        if body_path:
            self.add(path_str(body_path), line, CONF_EXPLICIT if elems else CONF_BARE)
        else:
            self.add("*", line, CONF_WHOLE_BODY)

    def _bind_callback(self, member: Node, item_path: tuple[str, ...], depth: int) -> bool:
        """Trace the item parameter of an inline callback. True if it could be traced."""
        call = member.parent
        if call is None or call.type != "call_expression":
            return False
        args = _args(call)
        callback = args[0] if args else None
        if callback is None or callback.type not in _FUNCTION_TYPES:
            return False
        pattern = _first_param_pattern(callback)
        if pattern is None:
            return False
        self.bind(pattern, "body", item_path, callback, depth + 1)
        return True


# ---------------------------------------------------------------- per-file analysis


@dataclass
class ScanResult:
    sites: list[CallSite] = field(default_factory=list)
    unanalyzed: list[UnanalyzedCall] = field(default_factory=list)


def _detect_call(node: Node, client_names: set[str]) -> tuple[str, str] | None:
    """If `node` is an HTTP call we understand, return (client, verb|'fetch')."""
    fn = node.child_by_field_name("function")
    # tree-sitter-typescript parses `await f<T>(x)` as a call whose function is `await f`.
    while fn is not None and fn.type == "await_expression" and fn.named_children:
        fn = fn.named_children[0]
    if fn is None:
        return None
    if fn.type == "identifier" and _text(fn) == "fetch":
        return "fetch", "fetch"
    if fn.type == "member_expression":
        obj = fn.child_by_field_name("object")
        prop = _text(fn.child_by_field_name("property")).lower()
        if obj is not None and obj.type == "identifier" and _text(obj) in client_names \
                and prop in HTTP_VERBS:
            return _text(obj), prop
    return None


def analyze_source(source: bytes, suffix: str, display_name: str,
                   client_names: set[str]) -> ScanResult:
    tree = _parser(suffix).parse(source)
    result = ScanResult()

    for node in _walk(tree.root_node):
        if node.type != "call_expression":
            continue
        detected = _detect_call(node, client_names)
        if detected is None:
            continue
        client, verb = detected
        args = _args(node)
        line = _line(node)

        raw = _static_string(args[0]) if args else None
        path = normalize_call_path(raw) if raw is not None else None
        if path is None:
            result.unanalyzed.append(
                UnanalyzedCall(display_name, line, f"{client} call with a non-static URL"))
            continue

        request_keys: set[str] | None = None
        if client == "fetch":
            options = args[1] if len(args) > 1 else None
            method_node = _object_pair(options, "method")
            method = _string_value(method_node).upper() if method_node is not None \
                and method_node.type == "string" else "GET"
            if method in BODY_METHODS:
                body = _object_pair(options, "body")
                if body is None:
                    request_keys = set()
                elif body.type == "call_expression" \
                        and _text(body.child_by_field_name("function")) == "JSON.stringify":
                    inner = _args(body)
                    request_keys = _object_keys(inner[0]) if inner else None
            kind = "fetch_response"
        else:
            method = verb.upper()
            if method in BODY_METHODS:
                request_keys = _object_keys(args[1]) if len(args) > 1 else set()
            kind = "response"

        tracer = _Tracer()
        tracer.trace(node, kind)
        result.sites.append(CallSite(
            file=display_name, line=line, method=method, path=path, client=client,
            usages=sorted(tracer.usages, key=lambda u: (u.line, u.field)),
            request_keys=request_keys,
        ))
    return result


def scan_frontend(root: str | Path, client_names: list[str] | set[str]) -> ScanResult:
    """Scan every .ts/.tsx file under `root` (skipping node_modules, dist, ...)."""
    base = Path(root)
    if not base.exists() or not base.is_dir():
        raise FrontendError(f"Frontend directory not found: {base}")

    names = set(client_names)
    display_root = base.resolve().parent
    combined = ScanResult()
    for path in sorted(base.rglob("*")):
        if path.suffix not in FRONTEND_SUFFIXES or path.name.endswith(".d.ts"):
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(base).parts):
            continue
        try:
            source = path.read_bytes()
        except OSError as exc:
            raise FrontendError(f"Could not read {path}: {exc}") from exc
        display = path.resolve().relative_to(display_root).as_posix()
        partial = analyze_source(source, path.suffix, display, names)
        combined.sites += partial.sites
        combined.unanalyzed += partial.unanalyzed
    return combined
