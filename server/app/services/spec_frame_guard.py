"""Recognize a narrow unsupported source-frame case, never produce a placement.

Only an explicitly owned plain-text mapping and an ordered local-axis declaration
can prove a reflection. This is a refusal guard, not a general frame interpreter.
"""

from __future__ import annotations

import ast
import re
import unicodedata
from fractions import Fraction
from typing import Any


def _normal(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace("−", "-")


def _plain_line(text: str, start: int, end: int) -> bool:
    """A whole source sentence at a line start, outside quoted JSON and fenced examples."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end < 0 else line_end
    if text[line_start:line_start + 1].isspace():
        return False  # indented code / structured-data scalars are not plain source statements
    if text[line_start:start].strip() or "\n" in text[start:end]:
        return False
    if any(c in text[line_start:line_end] for c in '{}"“”`'):
        return False
    tail = text[end:line_end].lstrip()
    if tail and tail[0] not in ".。":
        return False  # a truncated quote can omit a qualifier of the same statement
    fence: str | None = None
    for line in text[:line_start].splitlines():
        marker = line.strip()[:3]
        if marker in ("```", "~~~"):
            fence = None if marker == fence else marker if fence is None else fence
    return fence is None


def _owner(prefix: str, definitions: list[dict[str, Any]]) -> str | None:
    """Require a complete canonical name/alias with only explicit grammatical wrappers."""
    value = _normal(prefix).strip().casefold()
    owners: set[str] = set()
    for definition in definitions:
        for raw in (definition.get("name"), definition.get("number"), *(definition.get("aliases") or [])):
            name = _normal(str(raw or "")).strip().casefold()
            if not name:
                continue
            pattern = (r"(?:建立\s*|(?:define|establish)\s+)?"
                       r"(?:(?:[一二三四五六七八九十百]+|[0-9]+)\s*[根件个条]\s*)?"
                       + re.escape(name) + r"\s*(?:的)?")
            if re.fullmatch(pattern, value):
                owners.add(str(definition["key"]))
    return next(iter(owners)) if len(owners) == 1 else None


_DECLARATION = re.compile(
    r"(?P<owner>.+?)(?:局部(?:参数)?坐标(?:系)?|local (?:parameter )?coordinates?)\s*(?:为|are|is)?\s*"
    r"\(?([a-z])\s*[,、]\s*([a-z])\s*[,、]\s*([a-z])\)?(?=[:.。\s]|$)", re.I)
_MAPPING = re.compile(
    r"(?P<owner>.+?)(?:全局(?:放置|坐标)(?:关系|映射)|global (?:placement|coordinate) (?:mapping|relation))"
    r"\s*(?:为|is)?\s*:\s*X\s*=\s*(?P<x>[^,;]+)[,;]\s*Y\s*=\s*(?P<y>[^,;]+)[,;]\s*Z\s*=\s*(?P<z>[^,;。]+?)"
    r"(?:[,;](?:其中|where)\s*(?P<parameters>[^。]+?))?[。.]?$")
_STRUCTURED = re.compile(r"(?m)^\s*(?:[\{\[]|<[!?/A-Za-z]|[A-Za-z_][\w.-]*\s*:\s*[|>][-+]?\d?\s*$)")
_NUMBER = r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)"


def _independent_parameters(names: set[str], declaration: str | None) -> bool:
    """Only an explicit finite literal list proves an extra symbol independent of local axes."""
    if declaration is not None and len(declaration) > 120:
        return False
    if not names:
        return declaration is None
    if len(names) != 1 or declaration is None:
        return False
    name = next(iter(names))
    found = re.fullmatch(re.escape(name) + rf"\s*=\s*({_NUMBER}(?:\s*[,、]\s*{_NUMBER})*)", declaration.strip())
    return found is not None and all(abs(Fraction(value.strip())) <= 1_000_000_000
                                     for value in re.split("[,、]", found[1]))


def _linear(expression: str) -> dict[str, Fraction]:
    """Bounded exact affine arithmetic; callers must establish every symbol's meaning."""
    expression = expression.strip()
    if len(expression) > 120:
        raise ValueError("expression too long")
    expression = re.sub(r"(?<=[0-9])\s*(?=[a-z])", "*", expression)
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 60:
        raise ValueError("expression too complex")

    def scale(value: dict[str, Fraction], factor: Fraction) -> dict[str, Fraction]:
        return {key: number * factor for key, number in value.items() if number * factor}

    def read(node: ast.AST) -> dict[str, Fraction]:
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            literal = ast.get_source_segment(expression, node)
            if literal is None or re.fullmatch(_NUMBER, literal) is None:
                raise ValueError("number is not a bounded plain decimal literal")
            value = Fraction(literal)
            if abs(value) > 1_000_000_000:
                raise ValueError("constant out of range")
            return {"": value} if value else {}
        if isinstance(node, ast.Name) and re.fullmatch("[a-z]", node.id):
            return {node.id: Fraction(1)}
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return scale(read(node.operand), Fraction(-1 if isinstance(node.op, ast.USub) else 1))
        if isinstance(node, ast.BinOp):
            left, right = read(node.left), read(node.right)
            if isinstance(node.op, (ast.Add, ast.Sub)):
                factor = -1 if isinstance(node.op, ast.Sub) else 1
                result = {key: left.get(key, 0) + factor * right.get(key, 0) for key in left.keys() | right.keys()}
                return {key: value for key, value in result.items() if value}
            if isinstance(node.op, ast.Mult):
                if not (left.keys() - {""}):
                    return scale(right, left.get("", Fraction(0)))
                if not (right.keys() - {""}):
                    return scale(left, right.get("", Fraction(0)))
            if isinstance(node.op, ast.Div) and not (right.keys() - {""}) and right.get(""):
                return scale(left, 1 / right[""])
        raise ValueError("not bounded affine arithmetic")

    return read(tree.body)


def reflected_source_mapping(text: str, span: tuple[int, int], definition: str,
                             definitions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A proof of an explicitly owned signed-axis reflection, or no conclusion.

    Call only after verifying the original source/chunk/quote. Names alone do not
    select a source. Unrecognized syntax is not treated as an invalid transform.
    """
    start, end = span
    if _STRUCTURED.search(_normal(text)) or not _plain_line(text, start, end):
        return None
    match = _MAPPING.fullmatch(_normal(text[start:end]))
    if match is None or _owner(match["owner"], definitions) != definition:
        return None
    declarations: list[tuple[tuple[str, ...], int, int]] = []
    offset = 0
    for line in text[:start].splitlines(keepends=True):
        body = line.rstrip("\r\n")
        found = _DECLARATION.match(_normal(body))
        if found is not None and _plain_line(text, offset, offset + len(body)) and _owner(found["owner"], definitions) == definition:
            axes = found.group(2, 3, 4)
            if len(set(axes)) == 3 and all(re.fullmatch("[a-z]", axis) for axis in axes):
                declarations.append((axes, offset, offset + len(body)))
        offset += len(line)
    if len(declarations) != 1:
        return None
    axes, decl_start, decl_end = declarations[0]
    try:
        rows = [_linear(expression) for expression in match.group("x", "y", "z")]
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError):
        return None
    extra_symbols = set().union(*(row.keys() for row in rows)) - {"", *axes}
    if not _independent_parameters(extra_symbols, match["parameters"]):
        return None
    matrix = [[row.get(axis, Fraction(0)) for axis in axes] for row in rows]
    # This first guard proves signed permutations only, not arbitrary affine or
    # nonlinear maps, frame chains, mirror geometry or any usable Euler rotation.
    if any(sum(abs(value) for value in row) != 1 or any(value not in (-1, 0, 1) for value in row) for row in matrix):
        return None
    if any(sum(abs(matrix[row][col]) for row in range(3)) != 1 for col in range(3)):
        return None
    a, b, c = matrix
    determinant = a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0])
    if determinant != -1:
        return None
    return {"local_axes": list(axes), "target_axes": ["X", "Y", "Z"], "matrix": [[int(v) for v in row] for row in matrix],
            "determinant": -1, "declaration": text[decl_start:decl_end], "declaration_start": decl_start,
            "declaration_end": decl_end, "start": start, "end": end}
