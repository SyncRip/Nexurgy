"""Part identity, the frozen target of a spec lineage and its changes (generation reliability K;
.trellis/tasks/archive/2026-10/10-13-generation-reliability/design.md §2.5; brief WP04, AC10–AC12).

Why: in BHD1 runs a product title judged "not a part" still showed as a missing part, real definitions that hung nowhere
were mixed with titles, the alias merge could join parts whose names only look alike, and the target an assembly was
measured against changed between spec versions without a trace — completeness could rise by shrinking the target.

What (pure functions over the spec content; the service stores the results in the content of every finished version):

- :func:`identity`: one entry per candidate — ``{key, name, class, of, parent, quantity, drawing_no, reason, sources,
  decision}``; ``class`` is ``part`` / ``subassembly`` (under the root), ``alias`` (merged into ``of``), ``not_a_part``
  (a title, "—", a placeholder, or a recorded AI judgement: the exclusion audit, never a missing part) or
  ``catalog_alternative`` (a retained other variant, outside this product by a validated scope decision), or
  ``unresolved`` (real but hanging nowhere: a ``structure`` gap ``definition_unresolved``).
- :func:`merge_guard`: the deterministic rule every alias proposal passes (the AI's included): different drawing
  numbers, different series / size tags (B3 vs Ø60 / 60) or different key name characters (pressure plate vs pressure disc) are never merged
  unless a source sentence explicitly says they are the same part (:func:`explicit_same`).
- :func:`freeze`: the lineage's ``baseline`` (the target of its first finished version — the one every build of the
  lineage is measured against, frozen) carried into every later version, and ``target_history``: one entry per change of
  the target ``{from, to, field, old, new, reason, evidence, decision}`` (an alias merged, a definition hung, a revision,
  an answer). Coverage shows the baseline next to the current target, so a smaller target is always visible with its
  reason.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

CLASSES: tuple[str, ...] = ("part", "subassembly", "alias", "not_a_part", "catalog_alternative", "unresolved")

CATALOG_ONLY_FIELDS = {
    'quantity', 'title_block_quantity', 'drawing_quantity', 'total_quantity',
    'instance_quantity', 'component_quantity', 'member_quantity',
    'instance_count', 'component_count', 'member_count', 'name', 'number',
    'drawing_number', 'drawing_scale', 'drawing_format', 'sheet_count', 'title', 'sheet_size',
    'material', 'finish', 'standard_ref',
}


def catalog_scope_problem(content: dict[str, Any], child: str, target: str) -> str | None:
    """Structural eligibility only; original-source role proof belongs to the service."""
    from .assemblies import spec_tree

    defs = _defs(content)
    a, b = defs.get(child), defs.get(target)
    if not a or not b or child == target or child == content.get('root'):
        return 'Catalog scope needs two distinct known component definitions'
    if any(child in (i.get('definition'), i.get('parent')) for i in content.get('instances', [])):
        return 'A catalog alternative cannot have any parent or child membership'
    if target == content.get('root') or target not in spec_tree(content)['definitions']:
        return 'The selected variant must be reachable under the product root'
    if a.get('kind') != b.get('kind'):
        return 'Catalog alternatives must have the same component type'
    if not _core(a.get('name')) or _core(a.get('name')) != _core(b.get('name')):
        return 'Catalog and selected variant names must identify the same component type'
    fields = {str(f.get('subject', '')).partition('.')[2] for f in content.get('facts', [])
              if str(f.get('subject', '')).partition('.')[0] == child}
    if fields - CATALOG_ONLY_FIELDS:
        return 'A definition with independent geometric or engineering facts is not catalog-only'
    if a.get('template'):
        return 'A definition with a geometric template is not catalog-only'
    if any(i.get('owner') == child for i in content.get('interfaces', [])):
        return 'A definition with its own interfaces is not catalog-only'
    return None


def catalog_identity(definition: dict[str, Any]) -> dict[str, Any]:
    return {k: definition.get(k) for k in ('key', 'kind', 'name', 'number', 'size')}

# --- identity keys (generation reliability A, design §6.1) --------------------------------------------------------------

#: A standard designation (GB/T 93-2002, GB/T6170-2000, ISO 4017, DIN 933 …): it names a TYPE of part, never one part.
_STANDARD = r"(?:GB|JB|HG|QC|YB|SH|SJ|CB|HB|MT|ISO|DIN|EN|BS|JIS|ANSI|ASME|ASTM|SAE|IEC|NF|UNI|GOST)(?:\s*/\s*[TZ])?\s*\d"
STANDARD_NO = re.compile(rf"^\s*{_STANDARD}", re.IGNORECASE)
#: A standard designation anywhere in a text, with its number and year (left out of a name before its size tags are read).
STANDARD_IN_TEXT = re.compile(rf"(?<![A-Za-z]){_STANDARD}[\d.]*(?:\s*[-—–]\s*\d{{2,4}})?", re.IGNORECASE)


def is_standard_number(number: Any) -> bool:
    return bool(number) and STANDARD_NO.match(unicodedata.normalize("NFKC", str(number))) is not None


_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-", "－": "-"})


def _key_text(s: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s or "")).translate(_DASHES)).upper()


def size_of(size: Any, name: Any) -> str | None:
    """The size designation of a standard part: the stated one, else the series / size tags of its name ("spring washer 12"
    → 12, "bolt M12×30" → M12X30); None when neither gives one."""
    tags = sorted(series_tags(size)) if size not in (None, "") else []
    if not tags:
        tags = sorted(series_tags(STANDARD_IN_TEXT.sub(" ", unicodedata.normalize("NFKC", str(name or "")))))
    return "+".join(t.upper() for t in tags) or None


def identity_key(d: dict[str, Any], *, parent: str | None = None) -> str | None:
    """The identity of a component (design §6.1): its drawing number; for a standard part its standard number AND size
    (the standard alone is only the type: spring washers of size 12 / 8 / 10 of GB/T 93-2002 are three parts); else its name plus the
    parent it is used in (``parent``; None when no parent is known). ``d``: ``number``, ``size``, ``name``."""
    number = d.get("number")
    if number and not is_standard_number(number):
        return f"no:{_key_text(number)}"
    if number:
        size = size_of(d.get("size"), d.get("name"))
        if size:
            return f"std:{_key_text(number)}|{size}"
    name = _norm(d.get("name"))
    return f"name:{name}@{parent or ''}" if name else None


def compatible_standard_readings(a: str, b: str) -> bool:
    """Whether two normalized standard identities can be partial readings of ONE known key.

    This is not an alias rule: callers must already have the same explicit skeleton
    key, and must not index the partial identity for other mentions. Omitted editions
    and trailing size dimensions stay conflicting readings; different explicit
    editions, standard numbers and complete sizes remain distinct components.
    """
    if not a.startswith("std:") or not b.startswith("std:") or "|" not in a or "|" not in b:
        return False
    a_number, a_size = a[4:].split("|", 1)
    b_number, b_size = b[4:].split("|", 1)
    if a_number != b_number:
        # Full designation matching prevents ISO 401 from matching ISO 4017 or
        # GB/T 9 from matching GB/T 93. Only a parsed edition can be omitted.
        if not all(STANDARD_IN_TEXT.fullmatch(n) for n in (a_number, b_number)):
            return False
        a_base, _, a_year = a_number.partition("-")
        b_base, _, b_year = b_number.partition("-")
        if a_base != b_base or a_year and b_year and a_year != b_year:
            return False
    if a_size == b_size:
        return True
    # Reuse the normalized size-token rule (M12 fits M12X30, never M120).
    # Different sets of size alternatives are not one shortened designation.
    return "+" not in a_size and "+" not in b_size and not tags_conflict(series_tags(a_size), series_tags(b_size))


#: Changes of the target recorded per lineage (older entries are kept; the newest are cut past this).
MAX_HISTORY = 400
#: Target counts compared between versions.
TARGET_FIELDS = ("definitions", "instances", "subassemblies", "subassembly_instances", "recognized_definitions")

# --- not a part (deterministic: the exclusion audit) -------------------------------------------------------------------

#: A name that is only punctuation, a dash, a placeholder word or a question mark.
_PLACEHOLDER = re.compile(r"^[\s\-–—_~·.。,，、/\\|*?？!！()（）\[\]【】<>《》\"'“”‘’…]*$|"
                          r"^\s*(?:无|暂无|待定|待补|未知|未命名|空|略|同上|见上|n/?a|tbd|tba|none|null|unknown|untitled|xxx+|-+)\s*$",
                          re.IGNORECASE)
#: Document headings / titles that are not components.
_TITLE = re.compile(r"说明书|说明文档|技术要求|技术说明|明细表|零件清单|物料清单|目录|标题|封面|图纸目录|装配说明|设计说明|"
                    r"\b(?:title|document|readme|bill\s+of\s+materials|bom|table\s+of\s+contents)\b", re.IGNORECASE)


def _norm(s: Any) -> str:
    return re.sub(r"[\s()（）\[\]【】{}「」『』《》<>“”\"'‘’]+", "", unicodedata.normalize("NFKC", str(s or "")).casefold())


def placeholder_reason(d: dict[str, Any], content: dict[str, Any]) -> str | None:
    """Why a definition that hangs nowhere is no part at all, by rule (None: it may be a real part): an empty name / a
    dash / a placeholder word, a document heading, or the product title repeated (the root's name or the spec title)."""
    name = str(d.get("name") or "")
    if _PLACEHOLDER.match(name):
        return f"placeholder「{name.strip() or '（空）'}」: no component is named"
    if _TITLE.search(name):
        return f"document heading「{name.strip()}」: a title, not a component"
    root = next((x for x in content.get("definitions") or [] if isinstance(x, dict) and x.get("key") == content.get("root")), None)
    titles = {_norm(content.get("title"))} | ({_norm(root.get("name"))} if root else set())
    if _norm(name) and _norm(name) in titles - {""}:
        return f"product title「{name.strip()}」repeated: the product itself, not a component"
    return None


# --- the alias guard ---------------------------------------------------------------------------------------------------

#: Series / size tags in a name: a diameter (Ø60, φ8), a letter code with digits (B3, M8, M8x30, GB93), a bare number.
_TAG = re.compile(r"[øφ⌀]\s*\d+(?:\.\d+)?(?:\s*[x×*]\s*\d+(?:\.\d+)?)*|[a-z]{1,4}\d+(?:\.\d+)?(?:[x×*]\d+(?:\.\d+)?)*|\d+(?:\.\d+)?(?:[x×*]\d+(?:\.\d+)?)*")
#: An explicit statement that two names are one part, written between the two names (on the normalised text: no spaces
#: or brackets): "A (also called B)", "A, i.e. B", "A is B". Longer Chinese words containing the identity character are excluded; a bare "="
#: (a value) or a loose Chinese same-identity word elsewhere in the sentence never counts (review of K).
_SAME_BETWEEN = (r"(?:又称|又叫|也称|也叫|亦称|亦叫|简称|俗称|别称|别名|即(?!可|使|将|便|刻|时)|就是|等同于?|aka|alsocalled|alsoknownas|"
                 r"(?:is)?(?:the)?sameas|i\.e\.)(?:为|作)?")
#: "A and B are the same part": the two names joined, then the Chinese or English same-identity phrase right after the second.
_SAME_AND = r"(?:与|和|跟|及|同|、|and|&)"
_SAME_AFTER = (r"(?:均|都|实|实际上|其实)?(?:为|是|系|指)?(?:同一(?:个|件)?(?:零件|部件|零部件|标准件|件)?|"
               r"(?:are|is)?(?:the)?same(?:part|component|item)?)")
#: At most this many characters (no sentence end) between a name and the word ("pressure plate, i.e. pressure disc", "B3 plate, also called the Ø60 plate").
_SAME_GAP = r"[^。；;！!？?]{0,3}"
_NEGATION = re.compile(r"不|非|没|未|not|n't|nor", re.IGNORECASE)


def series_tags(name: Any) -> set[str]:
    """Series / size tags of a name, normalised ("Ø60" and "60" are one tag; "M8×30" = "m8x30")."""
    s = unicodedata.normalize("NFKC", str(name or "")).casefold().replace("Ø".casefold(), "ø")
    out = set()
    for m in _TAG.finditer(s):
        t = re.sub(r"^[øφ⌀]\s*", "", m.group(0)).replace("×", "x").replace("*", "x")
        out.add(re.sub(r"\s+", "", t))
    return out


def tags_conflict(a: set[str], b: set[str]) -> bool:
    """Both names carry tags and some tag of the one with fewer has no counterpart in the other (equal, or one the
    other's prefix before a size: "m8" fits "m8x30")."""
    if not a or not b:
        return False
    small, big = (a, b) if len(a) <= len(b) else (b, a)

    def fits(x: str, y: str) -> bool:
        return x == y or y.startswith(x + "x") or x.startswith(y + "x")

    return not all(any(fits(s, t) for t in big) for s in small)


def _number(n: Any) -> str:
    return re.sub(r"[\s\-_./]+", "", unicodedata.normalize("NFKC", str(n or "")).casefold())


def _core(name: Any) -> str:
    """The name without tags, brackets, spaces and punctuation: the key characters (CJK and words)."""
    s = unicodedata.normalize("NFKC", str(name or "")).casefold()
    s = _TAG.sub(" ", s)
    return "".join(ch for ch in s if ch.isalpha())


def _subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(ch in it for ch in short)


def _says_same(t: str, na: str, nb: str) -> bool:
    """The normalised sentence ``t`` says the names ``na`` / ``nb`` are one part: a same-word between them within a short
    gap (no negation in the gap), or the two joined and called the same part right after."""
    for x, y in ((na, nb), (nb, na)):
        ex, ey = re.escape(x), re.escape(y)
        for m in re.finditer(f"{ex}(?P<g1>{_SAME_GAP}){_SAME_BETWEEN}(?P<g2>{_SAME_GAP}){ey}", t, re.IGNORECASE):
            if not _NEGATION.search(m.group("g1") + m.group("g2")):
                return True
        if re.search(f"{ex}{_SAME_AND}{ey}{_SAME_AFTER}", t, re.IGNORECASE):
            return True
    return False


def explicit_same(sentences: list[dict[str, str]] | None, a: Any, b: Any) -> dict[str, str] | None:
    """A source sentence that names both and says they are one part ("pressure plate (also called pressure disc)", "pressure plate and pressure disc are the same part", "B3
    pressure plate, also called the Ø60 plate"), else None. The same-word has to stand between the two names: a sentence
    that only mentions both next to a longer Chinese word containing the identity character, a "=" value or a loose Chinese same-identity word is no such statement."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return None
    for s in sentences or []:
        t = _norm(s.get("text"))
        if na in t and nb in t and _says_same(t, na, nb):
            return s
    return None


def merge_guard(alias: dict[str, Any], target: dict[str, Any], sentences: list[dict[str, str]] | None = None) -> str | None:
    """Why two definitions must not be merged as one part (None: nothing speaks against it). Different drawing numbers,
    different series / size tags (:func:`tags_conflict`: B3 vs Ø60, M8 vs M10, M8x30 vs M8x40) or different key characters
    in names that look alike (the shorter name is not an abbreviation of the longer one: pressure plate vs pressure disc, base vs base plate; lock washer /
    spring washer and pressure disc / pressure disc (B3) pass) — unless a source sentence explicitly says they are the same part
    (:func:`explicit_same`)."""
    a_name, t_name = alias.get("name") or alias.get("key"), target.get("name") or target.get("key")
    why = None
    an, tn = alias.get("number"), target.get("number")
    if an and tn and _number(an) != _number(tn):
        why = f"different drawing numbers ({an} / {tn}) are never one part"
    if why is None:
        ta, tt = series_tags(a_name), series_tags(t_name)
        if tags_conflict(ta, tt):
            why = f"different series / size tags ({'、'.join(sorted(ta))} / {'、'.join(sorted(tt))}) in「{a_name}」and「{t_name}」"
    if why is None:
        # names that look alike (some key character shared) but are no abbreviation of one another; two names that share
        # nothing are not merged by their look at all (that rests on the decision's evidence)
        ca, ct = _core(a_name), _core(t_name)
        if ca and ct and set(ca) & set(ct):
            short, long = (ca, ct) if len(ca) <= len(ct) else (ct, ca)
            if not _subsequence(short, long):
                why = f"different key characters in「{a_name}」and「{t_name}」"
    if why is None:
        return None
    said = explicit_same(sentences, a_name, t_name)
    return None if said is not None else why + " (no source sentence says they are the same part)"


# --- identity ----------------------------------------------------------------------------------------------------------


def _defs(content: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {d["key"]: d for d in content.get("definitions") or [] if isinstance(d, dict) and d.get("key")}


def _sources_of(content: dict[str, Any], key: str, d: dict[str, Any]) -> list[str]:
    skel = next((e for e in content.get("skeleton") or [] if isinstance(e, dict) and e.get("key") == key), None)
    own = [str(s) for s in d.get("sources") or (skel or {}).get("sources") or [] if s]  # the skeleton line that declares it
    if own:
        return own[:5]
    out: list[str] = []
    for f in content.get("facts") or []:
        if isinstance(f, dict) and str(f.get("subject") or "").split(".")[0] == key:
            for e in f.get("evidence") or []:
                if isinstance(e, dict) and e.get("source"):
                    loc = f"{e.get('source')} · {e.get('locator')}" if e.get("locator") else str(e.get("source"))
                    if loc not in out:
                        out.append(loc)
    return out[:5]


def _decision(content: dict[str, Any], key: str) -> dict[str, Any] | None:
    """The AI structure decision on a definition (spec_aliases: identity = not a part, parent = unknown / hung)."""
    return next((a for a in content.get("assumptions") or [] if isinstance(a, dict) and a.get("subject") == key
                 and a.get("field") in ("identity", "parent") and a.get("author") == "ai_turbo"), None)


def identity(content: dict[str, Any], *, record: dict[str, Any] | None = None,
             parent: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One entry per candidate of the spec (an assembly root is not a part candidate). ``record``: the merge
    record of the version's alias resolution (aliases merged, proposals the guard or the checks refused); ``parent``: the
    parent version's identity (aliases merged there stay listed)."""
    from .assemblies import spec_tree
    from .engineering_spec import quantities

    defs = _defs(content)
    tree = spec_tree(content)
    root = tree["root"]
    qty = quantities(content)
    parents: dict[str, str] = {}
    for i in tree["instances"]:
        parents.setdefault(str(i.get("definition")), str(i.get("parent") or root))
    rejected = {str(r.get("key")): r for r in (record or {}).get("rejected") or [] if isinstance(r, dict)}
    catalog = {str(r.get('key')): r for r in (record or {}).get('catalog_exclusions') or []
               if isinstance(r, dict) and r.get('validated_catalog_scope') is True}
    for p in parent or []:
        if isinstance(p, dict) and p.get('class') == 'catalog_alternative' and p.get('validated_catalog_scope') is True:
            catalog.setdefault(str(p.get('key')), {**p, 'target': p.get('selected_variant'),
                                                  'definition': p.get('catalog_definition')})
    out: list[dict[str, Any]] = []
    for k, d in defs.items():
        if k == root and d.get("kind") not in {"part", "standard_part"}:
            continue
        e: dict[str, Any] = {"key": k, "name": str(d.get("name") or k), "class": "part", "of": None, "parent": parents.get(k),
                             "quantity": int((qty.get(k) or {}).get("total") or 0), "drawing_no": d.get("number"), "reason": None,
                             "sources": _sources_of(content, k, d), "decision": None}
        if k in tree["definitions"]:
            e["class"] = "subassembly" if d.get("kind") == "assembly" else "part"
        else:
            dec = _decision(content, k)
            rule = placeholder_reason(d, content)
            scope = catalog.get(k)
            scope_dec = next((a for a in content.get('assumptions') or []
                              if isinstance(a, dict) and scope and a.get('id') == scope.get('decision')
                              and a.get('subject') == k and a.get('field') == 'catalog_scope'
                              and a.get('author') == 'ai_turbo'
                              and a.get('adopted') == {'scope': 'outside_root', 'selected_variant': scope.get('target')}), None)
            if rule is not None:
                e.update({"class": "not_a_part", "reason": rule})
            elif (scope_dec and catalog_scope_problem(content, k, scope['target']) is None
                  and catalog_identity(d) == catalog_identity(scope.get('definition') or {})
                  and catalog_identity(defs[scope['target']]) == catalog_identity(scope.get('selected_definition') or {})):
                e.update({'class': 'catalog_alternative', 'selected_variant': scope['target'],
                          'validated_catalog_scope': True, 'reason': scope_dec.get('basis'),
                          'decision': scope_dec['id'], 'sources': list(scope_dec.get('sources') or []),
                          'catalog_definition': catalog_identity(d),
                          'selected_definition': catalog_identity(defs[scope['target']])})
            elif dec is not None and dec.get("field") == "identity":
                e.update({"class": "not_a_part", "reason": f"AI 判断：{dec.get('adopted')}；原因：{dec.get('basis')}", "decision": dec.get("id"),
                          "sources": list(dec.get("sources") or []) or e["sources"]})
            else:
                why = (f"AI 判断：{dec.get('adopted')}；原因：{dec.get('basis')}" if dec is not None
                       else f"not merged with {rejected[k].get('target')}: {rejected[k].get('reason')}" if k in rejected and rejected[k].get("target")
                       else "recognised in the sources but not under the root assembly")
                e.update({"class": "unresolved", "reason": why, "decision": dec.get("id") if dec is not None else None})
        out.append(e)
    seen = set(defs)
    for m in (record or {}).get("merged") or []:
        if not isinstance(m, dict) or m.get("alias") in seen:
            continue
        dec = next((a for a in content.get("assumptions") or [] if isinstance(a, dict) and a.get("id") == m.get("decision")), None)
        out.append({"key": m.get("alias"), "name": m.get("alias_name") or m.get("alias"), "class": "alias", "of": m.get("into"),
                    "parent": None, "quantity": 0, "drawing_no": None,
                    "reason": f"AI 判断：{dec.get('adopted')}；原因：{dec.get('basis')}" if dec else f"merged into {m.get('into')}",
                    "sources": list((dec or {}).get("sources") or []), "decision": m.get("decision")})
        seen.add(m.get("alias"))
    for p in parent or []:
        if isinstance(p, dict) and p.get("class") == "alias" and p.get("key") not in seen:
            out.append(dict(p))
            seen.add(p.get("key"))
    return out


def identity_of(content: dict[str, Any]) -> list[dict[str, Any]]:
    """The stored identity of a version, else computed now (versions from before generation reliability K)."""
    stored = content.get("identity")
    return stored if isinstance(stored, list) else identity(content)


def counts(entries: list[dict[str, Any]], unbound: int = 0) -> dict[str, int]:
    """The coverage block's ``identity``: parts, subassemblies, aliases, excluded (not a part), unresolved, catalog variants, and the names
    the sources mention that bound to no skeleton component (``unbound``: listed, never a definition)."""
    n = {c: sum(1 for e in entries if isinstance(e, dict) and e.get("class") == c) for c in CLASSES}
    return {"parts": n["part"], "subassemblies": n["subassembly"], "aliases": n["alias"], "excluded": n["not_a_part"],
            "unresolved": n["unresolved"], "unbound": unbound,
            **({'catalog_alternatives': n['catalog_alternative']} if n['catalog_alternative'] else {})}


def not_a_part_keys(content: dict[str, Any]) -> set[str]:
    return {str(e.get("key")) for e in identity_of(content) if isinstance(e, dict) and e.get("class") == "not_a_part"}


# --- baseline and target history -----------------------------------------------------------------------------------------


def target_summary(content: dict[str, Any]) -> dict[str, Any]:
    from .spec_coverage import targets

    t = targets(content)
    return {**{k: t[k] for k in TARGET_FIELDS}, "per_definition": dict(t["per_definition"])}


def _label(v: Any) -> str:
    return f"v{v.version_no}" if v is not None and getattr(v, "version_no", None) is not None else ""


def baseline_of(content: dict[str, Any]) -> dict[str, Any]:
    """The frozen baseline of a version, else its own target (versions from before generation reliability K)."""
    b = content.get("baseline")
    if isinstance(b, dict):
        return b
    t = target_summary(content)
    return {"spec_version_id": None, "label": "", **{k: t[k] for k in ("definitions", "instances", "subassemblies")}}


def freeze(content: dict[str, Any], *, version: Any, parent: Any = None, reason: str = "extract", evidence: list[str] | None = None,
           record: dict[str, Any] | None = None) -> dict[str, Any]:
    """The content with ``identity``, ``baseline`` and ``target_history`` (design §2.5). ``version``: the new version
    (its id and number); ``parent``: the nearest earlier version of the lineage with content (None: this is the first,
    whose target becomes the baseline); ``reason`` / ``evidence``: why the target may differ from the parent's (the
    rule, revise, answer …); ``record``: an alias-resolution / rule record (decisions per changed definition)."""
    pc = (parent.content or {}) if parent is not None else None
    out = dict(content)
    out["identity"] = identity(out, record=record, parent=(pc or {}).get("identity") if pc else None)
    now = target_summary(out)
    if not pc:
        out["baseline"] = {"spec_version_id": str(version.id) if getattr(version, "id", None) else None, "label": _label(version),
                           **{k: now[k] for k in ("definitions", "instances", "subassemblies")}}
        out["target_history"] = []
        return out
    out["baseline"] = dict(pc.get("baseline") or {"spec_version_id": str(parent.id), "label": _label(parent),
                                                    **{k: target_summary(pc)[k] for k in ("definitions", "instances", "subassemblies")}})
    before = target_summary(pc)
    decided = _decisions_by_key(record)
    base = {"from": _label(parent), "to": _label(version), "from_version_id": str(parent.id),
            "to_version_id": str(version.id) if getattr(version, "id", None) else None, "reason": reason, "evidence": list(evidence or [])}
    entries: list[dict[str, Any]] = []
    for k in TARGET_FIELDS:
        if before[k] != now[k]:
            entries.append({**base, "field": k, "old": before[k], "new": now[k], "decision": None})
    old_p, new_p = before["per_definition"], now["per_definition"]
    for k in sorted(set(old_p) | set(new_p)):
        o, n = old_p.get(k), new_p.get(k)
        if o == n:
            continue
        dec = decided.get(k)
        field = "definition" if o is None or n is None else "quantity"
        entries.append({**base, "field": f"{field}:{k}", "old": o, "new": n, "decision": dec.get("decision") if dec else None,
                        **({"evidence": list(dec.get("sources") or base["evidence"])} if dec and dec.get("sources") else {})})
    for m in (record or {}).get("merged") or []:
        if isinstance(m, dict) and m.get("alias") not in new_p:
            entries.append({**base, "field": f"alias:{m.get('alias')}", "old": m.get("alias_name") or m.get("alias"),
                            "new": f"{m.get('into')}「{m.get('into_name') or ''}」", "decision": m.get("decision")})
    history = [*list(pc.get("target_history") or []), *entries]
    out["target_history"] = history[-MAX_HISTORY:]
    return out


def _decisions_by_key(record: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for kind in ("hung", "merged", "not_a_part", "unknown"):
        for r in (record or {}).get(kind) or []:
            if isinstance(r, dict):
                out[str(r.get("key") or r.get("alias"))] = r
    return out
