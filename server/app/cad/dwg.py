"""Native DWG adapter: LibreDWG command-line tools run as separate processes.

Read (DWG -> sanitised DXF) and write (DXF -> DWG R2000) for the drawing
package. ``DWG_NOTES.md`` next to this file lists the runtime, the declared
versions, every normalisation (N*) and sanitising (S*) step and why, and the
licence boundary. The steps come from the W5a evidence report
(``.trellis/tasks/archive/2026-10/10-08-cad-ai/research/w5a-dwg-feasibility.md``).

Rules kept here:

- LibreDWG (GPL-3.0-or-later) only runs as an external process, in a fresh
  temporary directory, with a time budget. ``libredwg.so`` is never loaded
  into this process.
- Fail closed. Exit code 0 and an ``AC10xx`` magic are recorded, never the
  success criterion: :func:`write_dwg` returns only when the readback of the
  DWG matches the source DXF, and :func:`read_dwg` rejects truncated input,
  structural decoder errors and unstable output. Failures raise
  :class:`DwgError` with the steps that ran as evidence.
- A DXF is never renamed to DWG. The DXF returned by a read is a labelled,
  derived artefact with its own hash.

Pure library (no ORM / DB / service imports). Services pass
``settings.libredwg_bin_dir`` and ``settings.dwg_timeout_s``.
"""

from __future__ import annotations

import collections
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import time
import unicodedata
import zlib
from bisect import bisect_right
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import ezdxf
from ezdxf.entities.dimstyle import acdb_dimstyle
from ezdxf.math import Vec3

from .dxf_write import fixed_metadata
from .results import (
    DwgBlob,
    DwgQuarantinedEntity,
    DwgReadbackCheck,
    DwgReadResult,
    DwgRuntime,
    DwgStep,
    DwgTool,
    DwgWriteResult,
    Issue,
)

try:  # POSIX only: best-effort resource limits on the converter processes
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None  # type: ignore[assignment]

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "READ_VERSIONS",
    "SUPPORTED_LIBREDWG_VERSIONS",
    "WRITE_TARGETS",
    "DwgError",
    "detect_dwg_version",
    "dwg_runtime",
    "read_dwg",
    "require_dwg",
    "write_dwg",
]

LIBREDWG_TOOLS = ("dwg2dxf", "dxf2dwg", "dwgread", "dwgwrite")
#: LibreDWG releases the N*/S* recipe was verified with (W5a). Another release
#: may behave differently, so it is reported as unavailable until re-verified.
SUPPORTED_LIBREDWG_VERSIONS = ("0.14",)
#: Wall-clock budget of one read or write (all processes and checks together).
DEFAULT_TIMEOUT_S = 120.0
#: DWG versions :func:`write_dwg` can produce. Only R2000 passed the W5a gate.
WRITE_TARGETS = ("AC1015",)
#: DWG versions :func:`read_dwg` accepts, with their declaration status.
#: "provisional" until two external-author DWGs and an independent CAD editor
#: check exist; AC1021 (R2007) has not been tried with a real file.
READ_VERSIONS: dict[str, Literal["provisional", "untested"]] = {
    "AC1015": "provisional",
    "AC1018": "provisional",
    "AC1021": "untested",
    "AC1024": "provisional",
    "AC1027": "provisional",
    "AC1032": "provisional",
}
RELEASES = {
    "AC1009": "R11/R12",
    "AC1012": "R13",
    "AC1014": "R14",
    "AC1015": "R2000",
    "AC1018": "R2004",
    "AC1021": "R2007",
    "AC1024": "R2010",
    "AC1027": "R2013",
    "AC1032": "R2018",
}

DwgErrorCode = Literal[
    "dwg_runtime_unavailable",
    "dwg_not_dwg",
    "dwg_version_unsupported",
    "dwg_conversion_failed",
    "dwg_readback_mismatch",
    "dwg_timeout",
]

# Converter processes: best-effort limits (Linux prlimit), applied right after spawn.
_MEM_LIMIT = 4 << 30
_FILE_LIMIT = 2 << 30
#: Largest DWG read (the API upload limit is 50 MB) and largest DXF written to DWG.
MAX_DWG_BYTES = 64 << 20
MAX_DXF_BYTES = 128 << 20
#: Largest converter output read back into this process, checked (st_size) before reading: the
#: converter's DXF, its JSON object model, and any other output (DWG).
MAX_DXF_OUTPUT_BYTES = 128 << 20
MAX_JSON_OUTPUT_BYTES = 256 << 20
MAX_OUTPUT_BYTES = 128 << 20
# S6: how often dwg2dxf runs before control bytes in its output are fatal.
_S6_ATTEMPTS = 3
# S1: DXF text chunk length LibreDWG uses (characters).
_MTEXT_CHUNK = 250


class DwgError(Exception):
    """A DWG conversion that did not produce a trustworthy result.

    ``code`` is a :data:`DwgErrorCode`. ``details`` is JSON-safe; once a job
    started it holds the source identity and the steps that ran (evidence).
    """

    def __init__(self, code: DwgErrorCode, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_issue(self) -> Issue:
        return Issue(code=self.code, message=self.message, details=self.details)


# --------------------------------------------------------------------------- #
# Format detection and container checks
# --------------------------------------------------------------------------- #

_MAGIC_RE = re.compile(rb"AC10\d\d")
_ANCIENT_MAGIC = (b"MC0.0", b"AC1.2", b"AC1.40", b"AC1.50", b"AC2.10")
_DXF_TEXT_RE = re.compile(rb"\s*(?:0\s*\r?\n\s*SECTION|999\s*\r?\n)")


def detect_dwg_version(data: bytes) -> str | None:
    """The version string of a DWG file from its magic (e.g. ``"AC1032"``),
    or None when ``data`` does not start like a DWG file."""
    head = bytes(data[:6])
    if _MAGIC_RE.fullmatch(head):
        return head.decode("ascii")
    for magic in _ANCIENT_MAGIC:
        if head.startswith(magic):
            return magic.decode("ascii")
    return None


def _looks_like(data: bytes) -> str:
    if not data:
        return "empty"
    if data.startswith(b"AutoCAD Binary DXF"):
        return "binary_dxf"
    if _DXF_TEXT_RE.match(data[:512].lstrip(b"\xef\xbb\xbf")):
        return "dxf"
    return "unknown"


def require_dwg(data: bytes) -> str:
    """Return the version of DWG ``data`` or raise ``dwg_not_dwg``.

    DXF text, binary DXF or any other bytes are rejected whatever the file
    name says; the version itself is checked by the caller."""
    version = detect_dwg_version(data)
    if version is None:
        kind = _looks_like(data)
        raise DwgError("dwg_not_dwg", f"not a DWG file (content looks like: {kind})",
                       {"looks_like": kind, "size": len(data)})
    return version


def _r2004_key() -> bytes:
    seed, out = 1, bytearray()
    for _ in range(0x6C):
        seed = (seed * 0x343FD + 0x269EC3) & 0xFFFFFFFF
        out.append((seed >> 16) & 0xFF)
    return bytes(out)


_R2004_KEY = _r2004_key()


def _container_check(data: bytes, version: str) -> dict[str, Any]:
    """Compare the file length with the extent the DWG header declares.

    LibreDWG converts some truncated files with exit code 0 (W5b finding), so
    a file shorter than its own header says is rejected before conversion.
    R13-R2000 store section locators; R2004+ (except R2007) store an
    encrypted header with the address of the trailing second header.
    """
    info: dict[str, Any] = {"size": len(data), "layout": "not_checked", "declared_end": None}
    try:
        if version in ("AC1012", "AC1014", "AC1015"):
            info["layout"] = "r13_r2000_section_locators"
            count = struct.unpack_from("<l", data, 0x15)[0]
            if not 0 < count <= 16:
                raise DwgError("dwg_conversion_failed", "DWG section locator table is invalid", info)
            locators = [struct.unpack_from("<Bll", data, 0x19 + 9 * i) for i in range(count)]
            info["declared_end"] = max(addr + size for _, addr, size in locators)
        elif version in ("AC1018", "AC1024", "AC1027", "AC1032"):
            info["layout"] = "r2004_file_header"
            enc = data[0x80:0x80 + 0x6C]
            dec = bytes(a ^ b for a, b in zip(enc, _R2004_KEY))
            if len(dec) < 0x6C or not dec.startswith(b"AcFssFcAJMB\0"):
                raise DwgError("dwg_conversion_failed", "DWG R2004+ file header is unreadable", info)
            crc_ok = zlib.crc32(dec[:0x68] + b"\0\0\0\0") == struct.unpack_from("<L", dec, 0x68)[0]
            info["header_crc"] = "ok" if crc_ok else "mismatch"
            if crc_ok:  # field values are only trusted when the header checksum holds
                last_section_address, second_header = struct.unpack_from("<QQ", dec, 0x2C)
                second_header_end = second_header + 0x6C
                info["declared_end"] = max(last_section_address, second_header_end)
                # The page map includes allocated space; its end can exceed the physical file.
                # A complete copy of the CRC-checked header at the declared EOF proves this
                # compact layout. Keep the conservative bound if that copy is absent or damaged;
                # the converters below still check every section and reject structural errors.
                if (last_section_address > len(data) and second_header >= 0xEC
                        and second_header_end == len(data) and data[second_header:] == enc):
                    info["last_section_address"] = last_section_address
                    info["second_header"] = "matches_primary"
                    info["declared_end"] = second_header_end
    except struct.error as exc:
        raise DwgError("dwg_conversion_failed", "DWG file header is truncated", info) from exc
    if info["declared_end"] is not None and info["declared_end"] > len(data):
        raise DwgError("dwg_conversion_failed",
                       f"DWG file is truncated: {len(data)} bytes, header declares {info['declared_end']}", info)
    return info


# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #


def dwg_runtime(bin_dir: str | os.PathLike[str] | None = None) -> DwgRuntime:
    """Locate the LibreDWG tools and report their versions and build fingerprint.

    With ``bin_dir`` only that directory is searched (no PATH fallback), else
    PATH. Never raises: a missing tool or an unverified LibreDWG release gives
    ``available=False`` with a ``reason``. The probe is cached per directory
    and PATH value.
    """
    key = str(Path(bin_dir).expanduser().resolve()) if bin_dir else None
    return _probe_runtime(key, os.environ.get("PATH", "")).model_copy(deep=True)


@lru_cache(maxsize=8)
def _probe_runtime(bin_dir: str | None, path_env: str) -> DwgRuntime:
    tools: list[DwgTool] = []
    libraries: dict[str, str] = {}
    missing: list[str] = []
    for name in LIBREDWG_TOOLS:
        if bin_dir is not None:
            cand = Path(bin_dir) / name
            found = str(cand) if cand.is_file() and os.access(cand, os.X_OK) else None
        else:
            found = shutil.which(name, path=path_env or None)
        if found is None:
            missing.append(name)
            tools.append(DwgTool(name=name))
            continue
        real = Path(found).resolve()
        tools.append(DwgTool(name=name, path=str(real), version=_tool_version(real), sha256=_file_sha256(real)))
        libdir = real.parent.parent / "lib"
        if libdir.is_dir():
            for lib in sorted(libdir.glob("libredwg*")):
                lib_real = lib.resolve()
                if lib_real.suffix in (".a", ".la"):  # static / libtool files are not loaded at run time
                    continue
                if lib_real.is_file() and lib_real.name not in libraries:
                    libraries[lib_real.name] = _file_sha256(lib_real)

    versions = {t.version for t in tools if t.path}
    version = next(iter(versions)) if len(versions) == 1 else None
    reason = None
    if missing:
        where = f"in {bin_dir}" if bin_dir else "on PATH"
        reason = f"LibreDWG tools not found {where}: {', '.join(missing)}"
    elif version is None:
        reason = f"LibreDWG tool versions are unknown or inconsistent: {sorted(str(v) for v in versions)}"
    elif version not in SUPPORTED_LIBREDWG_VERSIONS:
        reason = (f"LibreDWG {version} is not a verified release "
                  f"(verified: {', '.join(SUPPORTED_LIBREDWG_VERSIONS)})")

    fingerprint = None
    if not missing:
        blob = json.dumps({"tools": {t.name: t.sha256 for t in tools}, "libraries": libraries}, sort_keys=True)
        fingerprint = hashlib.sha256(blob.encode()).hexdigest()[:16]

    independent = None
    if importlib.util.find_spec("ezdwg") is not None:
        try:
            independent = f"ezdwg {importlib.metadata.version('ezdwg')}"
        except importlib.metadata.PackageNotFoundError:  # pragma: no cover
            independent = "ezdwg (unknown version)"

    return DwgRuntime(
        available=reason is None,
        reason=reason,
        libredwg_version=version,
        tools=tools,
        libraries=libraries,
        host=f"{platform.system()}-{platform.machine()}",
        fingerprint=fingerprint,
        independent_reader=independent,
        ezdxf_version=ezdxf.__version__,
    )


def _tool_version(path: Path) -> str | None:
    try:
        proc = subprocess.run([str(path), "--version"], capture_output=True, timeout=15,
                              env=_tool_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or proc.stderr).decode("utf-8", "replace").strip()
    match = re.search(r"(\d+\.\d+(?:\.\d+)*)", text.splitlines()[0] if text else "")
    return match.group(1) if match else None


def _require_runtime(runtime: DwgRuntime | None) -> DwgRuntime:
    runtime = runtime or dwg_runtime()
    if not runtime.available:
        raise DwgError("dwg_runtime_unavailable", runtime.reason or "DWG runtime unavailable",
                       {"runtime": runtime.model_dump(mode="json")})
    return runtime


def _tool_env() -> dict[str, str]:
    """Minimal, locale-neutral environment for the converter processes."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C", "LANG": "C"}
    for key in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH", "SYSTEMROOT"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# Process runner
# --------------------------------------------------------------------------- #

_NUMBERS = re.compile(r"0x[0-9A-Fa-f]+|\d+")
_UNSTABLE = re.compile(r"^Warning: Unstable Class (?:object|entity) \d+ (\S+)")
# Decoder errors that mean the file structure is broken (truncation, bad
# sections, object stream overruns). Seen on truncated copies of the repo
# sample; none occurs on the complete file.
_FATAL_ERROR = re.compile(
    r"overflow|failed to|missing|out of bounds|truncat|decompress|\bcrc\b|"
    r"address \d+ \+ \d+ > \d+|invalid section|expected 0x[0-9a-f]+ (?:ENDBLK|SEQEND)",
    re.I,
)


def _top(lines: list[str], limit: int = 20) -> dict[str, int]:
    counter = collections.Counter(_NUMBERS.sub("#", line)[:160] for line in lines)
    return dict(counter.most_common(limit))


@dataclass
class _Job:
    runtime: DwgRuntime
    workdir: Path
    deadline: float
    steps: list[DwgStep] = field(default_factory=list)

    def _remaining(self, what: str) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise DwgError("dwg_timeout", f"DWG time budget exhausted before {what}", {"step": what})
        return left

    def check_time(self, what: str) -> None:
        self._remaining(what)

    def add(self, name: str, kind: Literal["transform", "check"], description: str, started: float,
            **counts: Any) -> DwgStep:
        step = DwgStep(name=name, kind=kind, description=description,
                       seconds=round(time.monotonic() - started, 3), counts=counts)
        self.steps.append(step)
        return step

    def run(self, tool: str, args: list[str], description: str) -> tuple[DwgStep, list[str]]:
        """Run one LibreDWG tool inside the job directory; return the step and
        its raw ERROR lines. Raises ``dwg_timeout`` past the budget."""
        timeout = self._remaining(tool)
        path = next((t.path for t in self.runtime.tools if t.name == tool), None)
        if path is None:
            raise DwgError("dwg_runtime_unavailable", f"LibreDWG tool {tool} is not available")
        started = time.monotonic()
        try:
            proc = subprocess.Popen([path, *args], cwd=self.workdir, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_tool_env(),
                                    start_new_session=True)
        except OSError as exc:  # tool removed or not executable since the runtime probe
            raise DwgError("dwg_runtime_unavailable", f"LibreDWG tool {tool} cannot be started: {exc}") from exc
        _limit_process(proc.pid)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.communicate()
            self.steps.append(DwgStep(name=tool, kind="process", description=description, argv=[tool, *args],
                                      seconds=round(time.monotonic() - started, 3),
                                      counts={"timeout_s": round(timeout, 3)}))
            raise DwgError("dwg_timeout", f"{tool} did not finish within the DWG time budget",
                           {"step": tool, "timeout_s": round(timeout, 3)}) from None
        text = (out + b"\n" + err).decode("utf-8", "replace")
        lines = text.splitlines()
        errors = [line for line in lines if line.startswith("ERROR")]
        warnings = [line for line in lines if line.startswith("Warning")]
        unstable = collections.Counter(m.group(1) for line in warnings if (m := _UNSTABLE.match(line)))
        step = DwgStep(name=tool, kind="process", description=description, argv=[tool, *args],
                       seconds=round(time.monotonic() - started, 3), exit_code=proc.returncode,
                       n_errors=len(errors), n_warnings=len(warnings), errors=_top(errors),
                       warnings=_top(warnings), unstable_classes=dict(unstable))
        self.steps.append(step)
        return step, errors

    def require_output(self, step: DwgStep, errors: list[str], output: Path, *, strict: bool,
                       max_bytes: int = MAX_OUTPUT_BYTES) -> bytes:
        """Fail closed on a process step: exit code, output present and, for
        ``strict`` steps, no ERROR line at all (else no structural ERROR). The
        output size is checked against ``max_bytes`` before it is read."""
        if step.exit_code != 0:
            raise DwgError("dwg_conversion_failed", f"{step.name} exited with code {step.exit_code}",
                           {"step": step.name, "errors": step.errors})
        bad = errors if strict else [e for e in errors if _FATAL_ERROR.search(e)]
        if bad:
            raise DwgError("dwg_conversion_failed", f"{step.name} reported {len(bad)} structural error(s)",
                           {"step": step.name, "errors": _top(bad)})
        if not output.is_file() or output.stat().st_size == 0:
            raise DwgError("dwg_conversion_failed", f"{step.name} wrote no output", {"step": step.name})
        size = output.stat().st_size
        if size > max_bytes:
            raise DwgError("dwg_conversion_failed",
                           f"{step.name} wrote {size} bytes ({output.name}), more than the {max_bytes} byte limit for this "
                           "output; the drawing is too large to convert safely",
                           {"step": step.name, "reason": "output_too_large", "bytes": size, "limit": max_bytes})
        return output.read_bytes()


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill the tool and anything it started (it leads its own session)."""
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    proc.kill()


def _limit_process(pid: int) -> None:
    prlimit = getattr(resource, "prlimit", None) if resource is not None else None
    if prlimit is None:  # macOS / Windows: only the time budget applies
        return
    for limit, value in ((resource.RLIMIT_AS, _MEM_LIMIT), (resource.RLIMIT_FSIZE, _FILE_LIMIT),
                         (resource.RLIMIT_CORE, 0)):
        try:
            prlimit(pid, limit, (value, value))
        except (OSError, ValueError):  # process already gone, or limit not allowed
            pass


# --------------------------------------------------------------------------- #
# DXF record index (byte level; DXF from LibreDWG can be tens of MB)
# --------------------------------------------------------------------------- #

# A record starts with a group-code-0 line followed by an upper-case type name.
# A value line "0" is always followed by a numeric code line, so this cannot
# match inside a record of a well-formed file.
_RECORD_RE = re.compile(rb"^[ \t]*0\r?\n([A-Z_][A-Z0-9_]*)\r?$", re.M)
_STRUCTURAL = {"SECTION", "ENDSEC", "TABLE", "ENDTAB", "EOF", "CLASS"}
_FLOAT_CODES = (set(range(10, 60)) | set(range(110, 150)) | set(range(210, 240)) | set(range(460, 470))
                | set(range(1010, 1060)))
_NONFINITE_LINE = re.compile(rb"^[ \t]*[-+]?(?:nan|inf|infinity)[ \t]*\r?$", re.M | re.I)
_CONTROL_RE = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _strip_cr(value: bytes) -> bytes:
    return value[:-1] if value.endswith(b"\r") else value


def _tag_index(lines: list[bytes], code: bytes) -> int | None:
    """Index of the value line of the first ``code`` tag in a record."""
    for k in range(0, len(lines) - 1, 2):
        if lines[k].strip() == code:
            return k + 1
    return None


def _tag_value(lines: list[bytes], code: bytes) -> bytes | None:
    idx = _tag_index(lines, code)
    return lines[idx].strip() if idx is not None else None


def _hex(value: bytes | None) -> int | None:
    try:
        return int(value, 16) if value else None
    except ValueError:
        return None


class _DxfRecords:
    """Records (group-code-0 blocks) of a DXF file with pending edits."""

    def __init__(self, raw: bytes):
        self.raw = raw
        self.crlf = b"\r\n" in raw[:4096]
        self.edits: dict[int, bytes | None] = {}
        self.starts: list[int] = []
        self.types: list[str] = []
        for match in _RECORD_RE.finditer(raw):
            self.starts.append(match.start())
            self.types.append(match.group(1).decode("ascii"))
        self.sections: list[str] = []
        section = ""
        for i, rtype in enumerate(self.types):
            if rtype == "SECTION":
                section = (_tag_value(self.lines(i), b"2") or b"").decode("ascii", "replace")
            self.sections.append(section)
            if rtype == "ENDSEC":
                section = ""

    def end(self, i: int) -> int:
        return self.starts[i + 1] if i + 1 < len(self.starts) else len(self.raw)

    def lines(self, i: int) -> list[bytes]:
        if i in self.edits and self.edits[i] is not None:
            return self.edits[i].split(b"\n")  # type: ignore[union-attr]
        return self.raw[self.starts[i]:self.end(i)].split(b"\n")

    def replace(self, i: int, lines: list[bytes]) -> None:
        self.edits[i] = b"\n".join(lines)

    def drop(self, i: int) -> None:
        self.edits[i] = None

    def record_at(self, offset: int) -> int:
        return bisect_right(self.starts, offset) - 1

    def output(self) -> bytes:
        parts, prev = [], 0
        for i in sorted(self.edits):
            parts.append(self.raw[prev:self.starts[i]])
            edit = self.edits[i]
            if edit is not None:
                parts.append(edit)
            prev = self.end(i)
        parts.append(self.raw[prev:])
        return b"".join(parts)

    def emitted(self) -> collections.Counter[str]:
        return collections.Counter(t for i, t in enumerate(self.types)
                                   if t not in _STRUCTURAL and not (i in self.edits and self.edits[i] is None))

    def describe(self, i: int) -> dict[str, Any]:
        lines = self.lines(i)
        return {"type": self.types[i], "section": self.sections[i],
                "handle": (_tag_value(lines, b"5") or b"").decode("ascii", "replace") or None}

    def value_code(self, offset: int) -> int | None:
        """Group code of the value line that starts at ``offset``."""
        end = offset - 1
        start = self.raw.rfind(b"\n", 0, max(end, 0)) + 1
        try:
            return int(self.raw[start:end].strip())
        except ValueError:
            return None


# --------------------------------------------------------------------------- #
# Read path: DWG -> DXF
# --------------------------------------------------------------------------- #


def read_dwg(data: bytes, *, runtime: DwgRuntime | None = None,
             timeout_s: float = DEFAULT_TIMEOUT_S) -> DwgReadResult:
    """Convert DWG bytes to a sanitised DXF with a provenance report.

    Checks the magic, the version and the declared file length, runs
    ``dwg2dxf`` and ``dwgread -O JSON`` in a fresh temporary directory, then
    applies S6 (control-byte guard), S1 (lossless MTEXT re-chunk), S2
    (anonymous-block relink by DWG handle), S3 (non-finite entity quarantine),
    S1b (non-UTF-8 bytes read as Latin-1, reported) and S5 (decoded-vs-emitted
    report), and loads the result with ezdxf (strict; ``recover`` only as a
    reported fallback). ``timeout_s`` bounds the whole call. Raises
    :class:`DwgError`; nothing partial is returned.
    """
    started = time.monotonic()
    data = bytes(data)
    if len(data) > MAX_DWG_BYTES:
        raise DwgError("dwg_conversion_failed", f"the DWG is {len(data)} bytes; files over {MAX_DWG_BYTES} bytes are not converted",
                       {"reason": "input_too_large", "bytes": len(data), "limit": MAX_DWG_BYTES})
    version = require_dwg(data)
    status = READ_VERSIONS.get(version)
    if status is None:
        raise DwgError("dwg_version_unsupported",
                       f"DWG {version} ({RELEASES.get(version, 'unknown release')}) is not a supported read version",
                       {"version": version, "supported": sorted(READ_VERSIONS)})
    source = DwgBlob(format="dwg", role="source", sha256=_sha(data), size=len(data), version=version)
    job: _Job | None = None
    try:
        container = _container_check(data, version)
        runtime = _require_runtime(runtime)
        with tempfile.TemporaryDirectory(prefix="partroute-dwg-") as tmp:
            job = _Job(runtime, Path(tmp), started + timeout_s)
            return _read(job, data, source, status, container, started)
    except DwgError as exc:
        exc.details.setdefault("source", source.model_dump(mode="json"))
        if job is not None:
            exc.details.setdefault("steps", [s.model_dump(mode="json") for s in job.steps])
        raise


def _read(job: _Job, data: bytes, source: DwgBlob, status: Literal["provisional", "untested"],
          container: dict[str, Any], started: float) -> DwgReadResult:
    warnings: list[Issue] = []
    losses: list[Issue] = []
    (job.workdir / "input.dwg").write_bytes(data)

    raw = _convert_with_guard(job)
    convert_step = next(s for s in reversed(job.steps) if s.name == "dwg2dxf")
    decoded = _decode_inventory(job)

    t0 = time.monotonic()
    records = _DxfRecords(raw)
    s1 = _rechunk_mtext(records)
    job.add("S1", "transform", "MTEXT 3/1 chunk runs cut inside a UTF-8 sequence or an escape: joined "
            "at byte level and re-split on character boundaries (bytes unchanged)", t0, **s1)
    job.check_time("S2")
    t0 = time.monotonic()
    s2 = _relink_blocks(records, decoded.links)
    job.add("S2", "transform", "duplicated anonymous block names renamed; DIMENSION/INSERT block "
            "references relinked by DWG handle (dwgread JSON)", t0, **s2)
    t0 = time.monotonic()
    quarantined, nonfinite_kept = _quarantine_nonfinite(records)
    job.add("S3", "transform", "entities with non-finite coordinates removed and reported (never zero-filled)",
            t0, quarantined=len(quarantined), handles=[q.handle for q in quarantined],
            nonfinite_values_outside_entities=len(nonfinite_kept))
    derived = records.output()
    emitted = records.emitted()
    del records
    t0 = time.monotonic()
    derived, s1b = _latin1_fallback(derived, utf8=_dxf_is_utf8(raw))
    job.add("S1b", "transform", "UTF-8 DXF only: bytes that are not UTF-8 read as Latin-1 (LibreDWG writes "
            "U+0080-U+00FF as single bytes; ezdwg decodes the same strings as those characters)", t0,
            **{k: v for k, v in s1b.items() if k != "samples"})
    if s1b["bytes"]:
        warnings.append(Issue(code="dwg_text_latin1_assumed",
                              message=f"{s1b['bytes']} text byte(s) were not UTF-8 and were read as Latin-1",
                              details=s1b))

    job.check_time("load")
    path = job.workdir / "derived.dxf"
    path.write_bytes(derived)
    doc, load_mode = _load_derived(job, path, warnings)
    job.check_time("checks")

    t0 = time.monotonic()
    links = _verify_links(doc, decoded.links)
    job.add("S2_check", "check", "DIMENSION/INSERT block references of the loaded DXF compared with the "
            "DWG handle links", t0, **links)
    t0 = time.monotonic()
    auditor = doc.audit()
    audit = {"errors": len(auditor.errors), "fixes": len(auditor.fixes),
             "error_samples": _top([str(e.message) for e in auditor.errors], 10)}
    job.add("audit", "check", "ezdxf audit of the loaded DXF (report only; the DXF bytes are not rewritten)",
            t0, **audit)
    t0 = time.monotonic()
    inventory = _inventory(doc)
    job.add("inventory", "check", "content inventory of the derived DXF", t0)

    t0 = time.monotonic()
    unstable = set()
    for step in job.steps:
        unstable.update(step.unstable_classes)
    rows, s5_losses, s5_warnings = _decoded_vs_emitted(decoded, emitted, quarantined, unstable)
    job.add("S5", "check", "objects decoded by LibreDWG (JSON) compared with records emitted in the DXF", t0,
            types_differing=len(rows), unexplained_entity_types=len(s5_losses))

    for q in quarantined:
        losses.append(Issue(code="dwg_entity_quarantined",
                            message=f"{q.type} {q.handle} removed: {q.reason}", details=q.model_dump(mode="json")))
    losses.extend(s5_losses)
    if links["mismatched"] or s2["links_unresolved"]:
        losses.append(Issue(code="dwg_block_link_unresolved",
                            message="some DIMENSION/INSERT entities do not reference the block the DWG links them to",
                            details={"mismatched": links["mismatched"], "unresolved": s2["links_unresolved"],
                                     "handles": links["mismatched_handles"]}))
    warnings.extend(s5_warnings)
    for kept in nonfinite_kept:
        warnings.append(Issue(code="dwg_nonfinite_value", message="non-finite value outside a drawable entity",
                              details=kept))
    if convert_step.n_errors:
        warnings.append(Issue(code="dwg_decoder_errors",
                              message=f"LibreDWG reported {convert_step.n_errors} non-structural decode error(s)",
                              details={"errors": convert_step.errors}))
    warnings.append(Issue(code=f"dwg_version_{status}",
                          message=f"DWG read support for {source.version} is {status}: not yet declared supported",
                          details={"version": source.version}))

    return DwgReadResult(
        source=source,
        version_status=status,
        container=container,
        runtime=job.runtime,
        steps=job.steps,
        load_mode=load_mode,
        raw_dxf=DwgBlob(format="dxf", role="raw_converter_output", sha256=_sha(raw), size=len(raw),
                        version=inventory.get("acadver")),
        derived_dxf=DwgBlob(format="dxf", role="derived", sha256=_sha(derived), size=len(derived),
                            version=inventory.get("acadver")),
        inventory={**inventory, "audit": audit},
        decoded_vs_emitted=rows,
        quarantined=quarantined,
        losses=losses,
        warnings=warnings,
        seconds=round(time.monotonic() - started, 3),
        dxf=derived,
    )


def _convert_with_guard(job: _Job) -> bytes:
    """dwg2dxf with the S6 guard: output holding C0 control bytes (seen once
    as a decoder over-read) is rejected and the conversion re-run."""
    out = job.workdir / "raw.dxf"
    rejected: list[dict[str, Any]] = []
    for attempt in range(1, _S6_ATTEMPTS + 1):
        out.unlink(missing_ok=True)
        step, errors = job.run("dwg2dxf", ["-y", "-o", "raw.dxf", "input.dwg"],
                               "DWG -> DXF (LibreDWG dwg2dxf)")
        raw = job.require_output(step, errors, out, strict=False, max_bytes=MAX_DXF_OUTPUT_BYTES)
        if not raw.rstrip().endswith(b"EOF"):
            raise DwgError("dwg_conversion_failed", "dwg2dxf output is incomplete (no EOF record)")
        t0 = time.monotonic()
        hits = [m.start() for m, _ in zip(_CONTROL_RE.finditer(raw), range(50))]
        step.counts.update(attempt=attempt, control_bytes=len(hits))
        if not hits:
            job.add("S6", "check", "C0 control-byte guard on the converter output (re-run on a hit)", t0,
                    attempts=attempt, rejected_runs=len(rejected))
            return raw
        records = _DxfRecords(raw)
        rejected.append({"attempt": attempt, "sha256": _sha(raw),
                         "locations": [records.describe(records.record_at(o)) for o in hits[:10]]})
        del records
    raise DwgError("dwg_conversion_failed",
                   f"converter output held control bytes in all {_S6_ATTEMPTS} runs (decoder over-read?)",
                   {"rejected": rejected})


@dataclass
class _Decoded:
    types: collections.Counter[tuple[str, str]]
    unreachable: collections.Counter[tuple[str, str]]
    links: dict[int, int]
    objects: int
    nonfinite_tokens: int


def _decode_inventory(job: _Job) -> _Decoded:
    out = job.workdir / "decoded.json"
    step, errors = job.run("dwgread", ["-O", "JSON", "-o", "decoded.json", "input.dwg"],
                           "DWG -> JSON object model (LibreDWG dwgread), for S2 links and S5")
    raw = job.require_output(step, errors, out, strict=False, max_bytes=MAX_JSON_OUTPUT_BYTES)
    out.unlink()
    t0 = time.monotonic()
    decoded = _parse_object_inventory(raw)
    del raw
    job.add("json_inventory", "check", "tolerant parse of the LibreDWG JSON (bare nan/inf tokens)", t0,
            objects=decoded.objects, links=len(decoded.links), nonfinite_tokens=decoded.nonfinite_tokens,
            unreachable=sum(decoded.unreachable.values()))
    return decoded


_JSON_NONFINITE = re.compile(rb"[nNiI][aAnN][nNfF]")


def _tolerant_json(raw: bytes) -> tuple[bytes, int]:
    """LibreDWG writes bare ``nan`` / ``-nan`` / ``inf``, which is not JSON."""
    parts, prev, count = [], 0, 0
    for match in _JSON_NONFINITE.finditer(raw):
        start, end = match.span()
        word = match.group().lower()
        if word not in (b"nan", b"inf"):
            continue
        negative = start > 0 and raw[start - 1:start] == b"-"
        lead = start - 1 if negative else start
        before = raw[lead - 1:lead] if lead > 0 else b" "
        after = raw[end:end + 1] or b" "
        if before not in b" \t\r\n[,:" or after not in b" \t\r\n,]":
            continue
        parts.append(raw[prev:lead])
        parts.append(b"NaN" if word == b"nan" else (b"-Infinity" if negative else b"Infinity"))
        prev, count = end, count + 1
    if not count:
        return raw, 0
    parts.append(raw[prev:])
    return b"".join(parts), count


def _ref(value: Any) -> int | None:
    """Absolute handle of a LibreDWG JSON handle reference ([code, size, value(, absolute)])."""
    if isinstance(value, list) and value and all(isinstance(v, int) for v in value):
        return value[-1] or None
    return None


def _parse_object_inventory(raw: bytes) -> _Decoded:
    fixed, nonfinite = _tolerant_json(raw)
    text = fixed.decode("utf-8", "replace")
    del fixed
    start = text.find('"OBJECTS"')
    if start < 0:
        raise DwgError("dwg_conversion_failed", "dwgread JSON has no OBJECTS section")
    pos = text.index("[", start) + 1
    decoder = json.JSONDecoder()
    types: collections.Counter[tuple[str, str]] = collections.Counter()
    owners: list[tuple[tuple[str, str], int | None]] = []
    handles: set[int] = set()
    links: dict[int, int] = {}
    size = len(text)
    try:
        while True:
            while pos < size and text[pos] in " \t\r\n,":
                pos += 1
            if pos >= size or text[pos] == "]":
                break
            obj, pos = decoder.raw_decode(text, pos)
            if not isinstance(obj, dict):
                continue
            kind, name = ("entity", obj.get("entity")) if obj.get("entity") else ("object", obj.get("object"))
            if not isinstance(name, str):
                continue
            key = (kind, name)
            types[key] += 1
            handle = _ref(obj.get("handle"))
            if handle is not None:
                handles.add(handle)
            owners.append((key, _ref(obj.get("ownerhandle"))))
            if kind == "entity" and handle is not None:
                if name.startswith("DIMENSION") or name == "LARGE_RADIAL_DIMENSION":
                    link = _ref(obj.get("block"))
                elif name in ("INSERT", "MINSERT"):
                    link = _ref(obj.get("block_header"))
                else:
                    link = None
                if link:
                    links[handle] = link
    except (json.JSONDecodeError, ValueError) as exc:
        raise DwgError("dwg_conversion_failed", f"dwgread JSON is not parseable: {exc}") from exc
    unreachable = collections.Counter(key for key, owner in owners if owner and owner not in handles)
    return _Decoded(types=types, unreachable=unreachable, links=links, objects=sum(types.values()),
                    nonfinite_tokens=nonfinite)


_PARTIAL_ESCAPE = re.compile(r"\\U\+(?![0-9A-Fa-f]{4})|\\M\+(?![0-9A-Fa-f]{5})")
_TEXT_TOKEN = re.compile(r"\\U\+[0-9A-Fa-f]{4}|\\M\+[0-9A-Fa-f]{5}|\\.|.", re.S)
_SURROGATE = re.compile("[\udc80-\udcff]")


def _needs_rechunk(parts: list[bytes], joined: str) -> bool:
    """True when a chunk boundary cuts a UTF-8 sequence or a ``\\U+`` / ``\\M+`` escape."""
    pieces = [p.decode("utf-8", "surrogateescape") for p in parts]
    return "".join(pieces) != joined or any(_PARTIAL_ESCAPE.search(p) for p in pieces)


def _split_text(text: str) -> list[str]:
    chunks, current = [], ""
    for token in _TEXT_TOKEN.findall(text):
        if len(current) + len(token) > _MTEXT_CHUNK:
            chunks.append(current)
            current = ""
        current += token
    chunks.append(current)
    return chunks


def _dxf_is_utf8(raw: bytes) -> bool:
    """DXF R2007+ (AC1021+) is UTF-8; older DXF uses $DWGCODEPAGE."""
    match = re.search(rb"\$ACADVER\r?\n[ \t]*1\r?\n[ \t]*(AC\d{4})", raw[:4096])
    return bool(match) and match.group(1) >= b"AC1021"


def _latin1_fallback(data: bytes, *, utf8: bool) -> tuple[bytes, dict[str, Any]]:
    """S1b. In a UTF-8 DXF, read every byte that is not part of valid UTF-8 as
    Latin-1 (reversible). Seen on the repo sample: ``IRHD45\\xb0-55\\xb0`` where
    ezdwg decodes ``IRHD45°-55°``. Not applied to codepage (pre-2007) DXF."""
    stats: dict[str, Any] = {"bytes": 0, "samples": []}
    if not utf8:
        stats["skipped"] = "not a UTF-8 DXF"
        return data, stats
    text = data.decode("utf-8", "surrogateescape")
    hits = [m.start() for m in _SURROGATE.finditer(text)]
    if not hits:
        return data, stats
    stats["bytes"] = len(hits)
    stats["samples"] = [_SURROGATE.sub(lambda m: f"<0x{ord(m.group()) - 0xDC00:02X}>", text[max(0, h - 30):h + 10])
                        for h in hits[:10]]
    fixed = _SURROGATE.sub(lambda m: chr(ord(m.group()) - 0xDC00), text)
    return fixed.encode("utf-8"), stats


def _rechunk_mtext(records: _DxfRecords) -> dict[str, int]:
    """S1. dwg2dxf splits long MTEXT into group-3 chunks by byte count, cutting
    UTF-8 sequences and ``\\U+XXXX`` escapes. Runs with a cut are joined and
    re-split on character boundaries; the bytes themselves are unchanged
    (undecodable bytes travel as surrogate escapes and are left to S1b)."""
    stats = {"mtext_runs": 0, "runs_rechunked": 0}
    cr = b"\r" if records.crlf else b""
    for i, rtype in enumerate(records.types):
        if rtype != "MTEXT":
            continue
        lines = records.lines(i)
        pairs = len(lines) // 2
        out: list[bytes] = []
        changed = False
        k = 0
        while k < pairs:
            if lines[2 * k].strip() == b"3":
                j = k
                while j < pairs and lines[2 * j].strip() == b"3":
                    j += 1
                if j < pairs and lines[2 * j].strip() == b"1":
                    stats["mtext_runs"] += 1
                    parts = [_strip_cr(lines[2 * x + 1]) for x in range(k, j + 1)]
                    joined = b"".join(parts).decode("utf-8", "surrogateescape")
                    if _needs_rechunk(parts, joined):
                        chunks = _split_text(joined)
                        for chunk in chunks[:-1]:
                            out += [lines[2 * k], chunk.encode("utf-8", "surrogateescape") + cr]
                        out += [lines[2 * j], chunks[-1].encode("utf-8", "surrogateescape") + cr]
                        stats["runs_rechunked"] += 1
                        changed = True
                        k = j + 1
                        continue
                    out.extend(lines[2 * k:2 * j + 2])
                    k = j + 1
                    continue
            out += [lines[2 * k], lines[2 * k + 1]]
            k += 1
        if changed:
            out.extend(lines[2 * pairs:])
            records.replace(i, out)
    return stats


_ANON_NUMBER = re.compile(rb"^\*([A-Za-z]+)(\d+)$")


def _relink_blocks(records: _DxfRecords, links: dict[int, int]) -> dict[str, Any]:
    """S2. dwg2dxf can give several anonymous blocks the same name (the DWG
    stores ``*D`` without numbers), so DIMENSIONs point at another dimension's
    graphics. Duplicates get fresh names; every DIMENSION / INSERT whose DWG
    link (dwgread JSON) is known is pointed at the block record with that handle."""
    names: dict[int, bytes] = {}
    name_line: dict[int, tuple[int, int]] = {}
    for i, rtype in enumerate(records.types):
        if rtype == "BLOCK_RECORD" and records.sections[i] == "TABLES":
            lines = records.lines(i)
            handle = _hex(_tag_value(lines, b"5"))
            idx = _tag_index(lines, b"2")
            if handle is not None and idx is not None:
                names[handle] = _strip_cr(lines[idx]).strip()
                name_line[handle] = (i, idx)

    groups: dict[bytes, list[int]] = collections.defaultdict(list)
    for handle in sorted(names):
        groups[names[handle].upper()].append(handle)
    taken = set(groups)
    numbers = [int(m.group(2)) for n in taken if (m := _ANON_NUMBER.match(n))]
    counter = max(numbers, default=0)
    renamed: dict[int, bytes] = {}
    skipped: list[str] = []
    duplicates = sorted(n.decode("utf-8", "replace") for n, hs in groups.items() if len(hs) > 1)
    for upper, handles in groups.items():
        if len(handles) < 2:
            continue
        if not upper.startswith(b"*") or upper.startswith((b"*MODEL_SPACE", b"*PAPER_SPACE")):
            skipped.append(upper.decode("utf-8", "replace"))
            continue
        letters = re.match(rb"^\*[A-Za-z]+", names[handles[0]])
        prefix = letters.group(0) if letters else b"*U"
        for handle in handles[1:]:  # the lowest handle keeps the name
            while True:
                counter += 1
                new = prefix + str(counter).encode()
                if new.upper() not in taken:
                    break
            taken.add(new.upper())
            renamed[handle] = new

    cr = b"\r" if records.crlf else b""
    for handle, new in renamed.items():
        i, idx = name_line[handle]
        lines = records.lines(i)
        lines[idx] = new + cr
        records.replace(i, lines)
        names[handle] = new

    blocks_renamed = 0
    relinked: collections.Counter[str] = collections.Counter()
    unresolved = ambiguous_before = wrong_before = 0
    for i, rtype in enumerate(records.types):
        if rtype == "BLOCK" and renamed:
            lines = records.lines(i)
            owner = _hex(_tag_value(lines, b"330"))
            if owner in renamed:
                for code in (b"2", b"3"):
                    idx = _tag_index(lines, code)
                    if idx is not None:
                        lines[idx] = renamed[owner] + cr
                records.replace(i, lines)
                blocks_renamed += 1
        elif rtype in ("DIMENSION", "INSERT", "LARGE_RADIAL_DIMENSION") and links:
            lines = records.lines(i)
            handle = _hex(_tag_value(lines, b"5"))
            target = links.get(handle) if handle is not None else None
            if target is None:
                continue
            idx = _tag_index(lines, b"2")
            if target not in names or idx is None:
                unresolved += 1
                continue
            current = _strip_cr(lines[idx]).strip().upper()
            holders = groups.get(current, [])
            if len(holders) > 1:
                ambiguous_before += 1
            elif holders != [target]:
                wrong_before += 1
            if current != names[target].upper():
                lines[idx] = names[target] + cr
                records.replace(i, lines)
                relinked[rtype] += 1
    return {"duplicate_names": duplicates, "records_renamed": len(renamed), "blocks_renamed": blocks_renamed,
            "relinked": dict(relinked), "links_known": len(links), "links_unresolved": unresolved,
            "links_ambiguous_before": ambiguous_before, "links_wrong_before": wrong_before,
            "duplicates_not_renamed": skipped}


def _quarantine_nonfinite(records: _DxfRecords) -> tuple[list[DwgQuarantinedEntity], list[dict[str, Any]]]:
    """S3. Non-finite numbers (``nan`` / ``-nan`` / ``inf``) in a float group of
    a drawable entity remove that entity (with its VERTEX / ATTRIB / SEQEND
    children) from the derived DXF. Values elsewhere are reported only."""
    hits: dict[int, set[int]] = collections.defaultdict(set)
    for match in _NONFINITE_LINE.finditer(records.raw):
        i = records.record_at(match.start())
        code = records.value_code(match.start())
        if i >= 0 and code in _FLOAT_CODES:
            hits[i].add(code)

    quarantined: list[DwgQuarantinedEntity] = []
    kept: list[dict[str, Any]] = []
    children = ("VERTEX", "ATTRIB", "SEQEND")
    done: set[int] = set()
    for i in sorted(hits):
        rtype = records.types[i]
        if records.sections[i] not in ("ENTITIES", "BLOCKS") or rtype in ("BLOCK", "ENDBLK"):
            kept.append({**records.describe(i), "codes": sorted(hits[i])})
            continue
        head = i
        if rtype in children:
            while head > 0 and records.types[head] in children:
                head -= 1
            if records.types[head] not in ("POLYLINE", "INSERT"):
                head = i
        tail = head + 1
        if records.types[head] in ("POLYLINE", "INSERT"):
            while tail < len(records.types) and records.types[tail] in children:
                tail += 1
                if records.types[tail - 1] == "SEQEND":
                    break
        if head in done:
            continue
        lines = records.lines(head)
        for j in range(head, tail):
            records.drop(j)
            done.add(j)
        quarantined.append(DwgQuarantinedEntity(
            handle=(_tag_value(lines, b"5") or b"?").decode("ascii", "replace"),
            type=records.types[head],
            layer=(_tag_value(lines, b"8") or b"").decode("utf-8", "replace") or None,
            section=records.sections[head],
            reason="non-finite coordinate value",
            codes=sorted(set().union(*(hits.get(j, set()) for j in range(head, tail)))),
        ))
    return quarantined, kept


def _load_derived(job: _Job, path: Path, warnings: list[Issue]) -> tuple[Any, Literal["strict", "recover"]]:
    t0 = time.monotonic()
    try:
        doc = ezdxf.readfile(path)
    except Exception as exc:  # ezdxf raises several types for malformed DXF
        strict_error = f"{type(exc).__name__}: {str(exc)[:300]}"
    else:
        job.add("load", "check", "ezdxf strict load of the derived DXF", t0, mode="strict")
        return doc, "strict"
    from ezdxf import recover

    try:
        doc, auditor = recover.readfile(path)
    except Exception as exc:
        raise DwgError("dwg_conversion_failed", "derived DXF does not load (strict or recover)",
                       {"strict_error": strict_error, "recover_error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                       ) from exc
    recover_errors = _top([str(e.message) for e in auditor.errors], 10)
    job.add("load", "check", "ezdxf load of the derived DXF: strict failed, recover used", t0, mode="recover",
            strict_error=strict_error, recover_errors=recover_errors)
    warnings.append(Issue(code="dwg_recover_load", message="derived DXF only loads in ezdxf recover mode",
                          details={"strict_error": strict_error, "recover_errors": recover_errors}))
    return doc, "recover"


def _verify_links(doc: Any, links: dict[int, int]) -> dict[str, Any]:
    checked = matching = 0
    mismatched: list[str] = []
    for entity in doc.entitydb.values():
        rtype = entity.dxftype()
        if rtype not in ("DIMENSION", "INSERT"):
            continue
        target = links.get(int(entity.dxf.handle, 16))
        if target is None:
            continue
        checked += 1
        name = entity.dxf.get("geometry") if rtype == "DIMENSION" else entity.dxf.get("name")
        block = doc.blocks.get(name) if name else None
        if block is not None and int(block.block_record_handle, 16) == target:
            matching += 1
        else:
            mismatched.append(entity.dxf.handle)
    return {"checked": checked, "matching": matching, "mismatched": len(mismatched),
            "mismatched_handles": mismatched[:20]}


def _inventory(doc: Any) -> dict[str, Any]:
    msp = doc.modelspace()
    dims = [e for e in msp if e.dxftype() == "DIMENSION"]
    measurable = 0
    for dim in dims:
        try:
            dim.get_measurement()
            measurable += 1
        except Exception:  # a broken dimension only lowers the count
            pass
    inventory: dict[str, Any] = {
        "acadver": doc.dxfversion,
        "dwgcodepage": doc.header.get("$DWGCODEPAGE"),
        "insunits": doc.header.get("$INSUNITS"),
        "measurement": doc.header.get("$MEASUREMENT"),
        "msp_entities": len(msp),
        "msp_types": dict(collections.Counter(e.dxftype() for e in msp).most_common()),
        "layouts": {layout.name: len(layout) for layout in doc.layouts},
        "layers": len(doc.layers),
        "blocks": len(doc.blocks),
        "text_styles": len(doc.styles),
        "dimstyles": len(doc.dimstyles),
        "fonts": sorted({s.dxf.get("font", "") for s in doc.styles} - {""})[:30],
        "dimensions": {
            "count": len(dims),
            "by_dimtype": {str(k): v for k, v in sorted(collections.Counter(d.dimtype for d in dims).items())},
            "measurement_computable": measurable,
            "with_geometry_block": sum(1 for d in dims if d.dxf.get("geometry") in doc.blocks),
        },
        "proxy_entities": sum(1 for e in doc.entitydb.values()
                              if e.dxftype() in ("ACAD_PROXY_ENTITY", "ACAD_PROXY_OBJECT")),
    }
    try:
        from ezdxf import bbox

        ext = bbox.extents(msp, fast=True)
        inventory["extents"] = [[round(ext.extmin.x, 4), round(ext.extmin.y, 4)],
                                [round(ext.extmax.x, 4), round(ext.extmax.y, 4)]] if ext.has_data else None
    except Exception as exc:  # e.g. no fonts on the host (S8)
        inventory["extents"] = None
        inventory["extents_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return inventory


_TYPE_ALIASES = {"BLOCK_HEADER": "BLOCK_RECORD"}
# LibreDWG marks these classes unstable; their DXF output differs between builds (W5a S5).
_UNSTABLE_CLASSES = {"DIMASSOC", "LARGERADIALDIMENSION", "MATERIAL", "MLEADERSTYLE", "TABLESTYLE",
                     "EVALUATIONGRAPH", "MENTALRAYRENDERSETTINGS"}


def _canon(name: str) -> str:
    """Common key for LibreDWG JSON names and DXF record names (partial map)."""
    upper = name.upper()
    for prefix, alias in (("DIMENSION_", "DIMENSION"), ("POLYLINE_", "POLYLINE"), ("VERTEX_", "VERTEX")):
        if upper.startswith(prefix):
            return alias
    upper = _TYPE_ALIASES.get(upper, upper)
    for prefix in ("ACDB_", "ACDB", "ACAD_"):
        if upper.startswith(prefix) and len(upper) > len(prefix):
            upper = upper[len(prefix):]
            break
    if upper.endswith("_VERSION"):
        upper = upper[:-8]
    return upper.replace("_", "")


def _decoded_vs_emitted(decoded: _Decoded, emitted: collections.Counter[str],
                        quarantined: list[DwgQuarantinedEntity],
                        unstable_log: set[str]) -> tuple[list[dict[str, Any]], list[Issue], list[Issue]]:
    """S5 (+S4). Objects LibreDWG decoded compared with records in the DXF.

    ``unreachable`` objects (owner not in the file) are stale data (S4).
    Classes LibreDWG marks unstable are never relied on: entities of such a
    class (jogged-radius dimensions) are reported as unsupported content
    whether or not this build wrote them, objects (DIMASSOC, ASSOC*) as a
    warning. Any other entity decoded but not written is a loss.
    """
    dec: collections.Counter[str] = collections.Counter()
    unr: collections.Counter[str] = collections.Counter()
    kinds: dict[str, str] = {}
    for (kind, name), n in decoded.types.items():
        if name.endswith("_CONTROL"):
            continue
        dec[_canon(name)] += n
        kinds.setdefault(_canon(name), kind)
    for (_, name), n in decoded.unreachable.items():
        unr[_canon(name)] += n
    emi: collections.Counter[str] = collections.Counter()
    for name, n in emitted.items():
        emi[_canon(name)] += n
    quar = collections.Counter(_canon(q.type) for q in quarantined)
    unstable = _UNSTABLE_CLASSES | {_canon(n) for n in unstable_log}

    rows: list[dict[str, Any]] = []
    losses: list[Issue] = []
    unstable_objects: dict[str, list[int]] = {}
    unreachable: dict[str, int] = {}
    for key in sorted(set(dec) | set(emi)):
        d, e = dec[key], emi[key]
        is_unstable = (key in unstable or key.startswith("ASSOC")) and d > 0
        if d == e and not quar[key] and not is_unstable:
            continue
        row = {"type": key, "kind": kinds.get(key, "unknown"), "decoded": d, "emitted": e,
               "unreachable": unr[key], "quarantined": quar[key], "unexplained": d - unr[key] - quar[key] - e,
               "unstable_class": is_unstable}
        rows.append(row)
        if unr[key]:
            unreachable[key] = unr[key]
        if is_unstable and row["kind"] == "entity":
            losses.append(Issue(code="dwg_unsupported_entity",
                                message=f"{d} {key}: LibreDWG marks this entity class unstable and builds differ "
                                        f"({e} written to the DXF here); not supported, do not rely on it",
                                details=row))
        elif is_unstable:
            unstable_objects[key] = [d, e]
        elif row["kind"] == "entity" and row["unexplained"] > 0:
            losses.append(Issue(code="dwg_decoded_not_emitted",
                                message=f"{row['unexplained']} {key} decoded from the DWG are missing in the DXF",
                                details=row))
    warnings = []
    if unreachable:
        warnings.append(Issue(code="dwg_unreachable_objects",
                              message="objects decoded with an owner that is not in the file "
                                      "(stale data, not drawing content)",
                              details={"by_type": unreachable}))
    if unstable_objects:
        warnings.append(Issue(code="dwg_unstable_classes",
                              message="non-graphical classes LibreDWG marks unstable (associativity, materials, "
                                      "styles); not relied on",
                              details={"decoded_emitted": unstable_objects}))
    return rows, losses, warnings


# --------------------------------------------------------------------------- #
# Write path: DXF -> DWG R2000
# --------------------------------------------------------------------------- #


def write_dwg(dxf: bytes, *, target: str = "AC1015", runtime: DwgRuntime | None = None,
              timeout_s: float = DEFAULT_TIMEOUT_S, require_independent: bool = True,
              readback: bool = True, semantic_readback: bool | None = None) -> DwgWriteResult:
    """Convert DXF bytes (as written by ezdxf / ``make_drawing``) to DWG R2000.

    Normalisation N1-N7 (+N2b), ``dxf2dwg --as r2000``, the ``dwgread`` JSON
    pass N8/N8b and ``dwgwrite``; then a strict LibreDWG readback and an
    independent ezdwg readback are compared with signatures of the source
    DXF. Any mismatch raises ``dwg_readback_mismatch``; only ``AC1015`` is a
    target. With ``require_independent=False`` a host without ezdwg still
    writes, the ezdwg check is ``skipped`` and ``verified`` is False.

    ``readback=False`` (execution policy ``turbo``: the semantic readback is a
    business check) still runs the whole conversion and fails closed on a
    converter error, an empty output or a version other than ``target``; both
    readback checks are ``skipped`` and ``verified`` is False.
    ``semantic_readback`` is an alias of ``readback`` (W7 callers); when given it wins.
    """
    started = time.monotonic()
    if target not in WRITE_TARGETS:
        raise DwgError("dwg_version_unsupported",
                       f"DWG write target {target} is not supported (supported: {', '.join(WRITE_TARGETS)})",
                       {"target": target, "supported": list(WRITE_TARGETS)})
    data = bytes(dxf)
    if not data or detect_dwg_version(data) is not None:
        raise DwgError("dwg_conversion_failed", "write_dwg expects DXF bytes",
                       {"looks_like": "dwg" if data else "empty"})
    if len(data) > MAX_DXF_BYTES:
        raise DwgError("dwg_conversion_failed", f"the DXF is {len(data)} bytes; files over {MAX_DXF_BYTES} bytes are not converted",
                       {"reason": "input_too_large", "bytes": len(data), "limit": MAX_DXF_BYTES})
    source = DwgBlob(format="dxf", role="source", sha256=_sha(data), size=len(data))
    job: _Job | None = None
    try:
        runtime = _require_runtime(runtime)
        if readback and require_independent and not runtime.independent_reader:
            raise DwgError("dwg_runtime_unavailable",
                           "the independent DWG reader (ezdwg) is not installed on this host",
                           {"runtime": runtime.model_dump(mode="json")})
        with tempfile.TemporaryDirectory(prefix="partroute-dwg-") as tmp:
            job = _Job(runtime, Path(tmp), started + timeout_s)
            return _write(job, data, source, started, readback=readback if semantic_readback is None else semantic_readback)
    except DwgError as exc:
        exc.details.setdefault("source", source.model_dump(mode="json"))
        if job is not None:
            exc.details.setdefault("steps", [s.model_dump(mode="json") for s in job.steps])
        raise


def readback_dwg(dxf: bytes, dwg: bytes, *, runtime: DwgRuntime | None = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S) -> list[DwgReadbackCheck]:
    """Semantic readback of an already written DWG against the DXF it was written from: the same two readers as
    :func:`write_dwg` (LibreDWG, then the independent ezdwg reader; ``skipped`` when ezdwg is not installed). Used to
    verify a Turbo DWG later on its exact bytes (W7, AC-T08). Raises :class:`DwgError` without a runtime or for a DXF
    that does not load."""
    started = time.monotonic()
    runtime = _require_runtime(runtime)
    with tempfile.TemporaryDirectory(prefix="partroute-dwg-") as tmp:
        job = _Job(runtime, Path(tmp), started + timeout_s)
        src_path = job.workdir / "source.dxf"
        src_path.write_bytes(bytes(dxf))
        try:
            doc = ezdxf.readfile(src_path)
        except Exception as exc:  # ezdxf raises several types for malformed DXF
            raise DwgError("dwg_conversion_failed", f"source DXF does not load strictly: {type(exc).__name__}",
                           {"error": str(exc)[:300]}) from exc
        _carry_cjk_text(job, doc)  # the DWG was written from the restructured text (N0), compare with that
        sig = _signature(doc)
        (job.workdir / "output.dwg").write_bytes(bytes(dwg))
        job.check_time("readback")
        libre, _ = _readback_libredwg(job, sig)
        job.check_time("independent readback")
        return [libre, _readback_ezdwg(job, sig)]


def _write(job: _Job, data: bytes, source: DwgBlob, started: float, *, readback: bool = True) -> DwgWriteResult:
    warnings: list[Issue] = []
    src_path = job.workdir / "source.dxf"
    src_path.write_bytes(data)
    t0 = time.monotonic()
    try:
        doc = ezdxf.readfile(src_path)
    except Exception as exc:  # ezdxf raises several types for malformed DXF
        raise DwgError("dwg_conversion_failed", f"source DXF does not load strictly: {type(exc).__name__}",
                       {"error": str(exc)[:300]}) from exc
    source.version = doc.dxfversion
    cjk = _carry_cjk_text(job, doc)
    sig = _signature(doc)
    job.add("source", "check", "strict load and signature of the source DXF", t0,
            dxfversion=doc.dxfversion, entities=sum(sig.full.values()), dimensions=len(sig.dims),
            texts=len(sig.texts))

    norm = {**_normalise(job, doc), **cjk}
    if norm["linetypes_added"]:
        warnings.append(Issue(code="dwg_source_repair",
                              message="layers referenced undefined linetypes; definitions were added (N3)",
                              details={"linetypes": norm["linetypes_added"]}))
    intermediate_bytes = _save_r2000(job, doc)
    del doc
    (job.workdir / "intermediate.dxf").write_bytes(intermediate_bytes)
    intermediate = DwgBlob(format="dxf", role="intermediate", sha256=_sha(intermediate_bytes),
                           size=len(intermediate_bytes), version="AC1015")

    step, errors = job.run("dxf2dwg", ["-y", "--as", "r2000", "-o", "converted.dwg", "intermediate.dxf"],
                           "normalised DXF R2000 -> DWG R2000 (LibreDWG dxf2dwg)")
    job.require_output(step, errors, job.workdir / "converted.dwg", strict=True)
    step, errors = job.run("dwgread", ["-O", "JSON", "-o", "converted.json", "converted.dwg"],
                           "DWG -> JSON object model (LibreDWG dwgread), for N8/N8b")
    model = job.require_output(step, errors, job.workdir / "converted.json", strict=True, max_bytes=MAX_JSON_OUTPUT_BYTES)
    (job.workdir / "patched.json").write_bytes(_patch_json(job, model))
    step, errors = job.run("dwgwrite", ["-y", "-I", "JSON", "--as", "r2000", "-o", "output.dwg", "patched.json"],
                           "patched JSON -> DWG R2000 (LibreDWG dwgwrite)")
    dwg = job.require_output(step, errors, job.workdir / "output.dwg", strict=True)

    t0 = time.monotonic()
    version = detect_dwg_version(dwg)
    if version != "AC1015":
        raise DwgError("dwg_conversion_failed", f"converter wrote {version or 'no DWG'} instead of AC1015")
    container = _container_check(dwg, version)
    job.add("container", "check", "output magic and section extents (recorded; not the success criterion)",
            t0, version=version, **container)

    if not readback:
        reason = "semantic readback not requested (execution policy turbo)"
        skipped = [DwgReadbackCheck(reader="libredwg", reader_version=job.runtime.libredwg_version, status="skipped",
                                    reason=reason, entities=sum(sig.full.values()), dimensions=len(sig.dims),
                                    texts=len(sig.texts)),
                   DwgReadbackCheck(reader="ezdwg", status="skipped", reason=reason, entities=sum(sig.short.values()),
                                    dimensions=len(sig.dims), texts=len(sig.texts))]
        warnings.append(Issue(code="dwg_readback_skipped", message=reason))
        return DwgWriteResult(
            target="AC1015", source=source, intermediate=intermediate,
            output=DwgBlob(format="dwg", role="output", sha256=_sha(dwg), size=len(dwg), version=version),
            runtime=job.runtime, steps=job.steps, readback=skipped, verified=False,
            losses=_write_losses(norm, sig, None), warnings=warnings,
            seconds=round(time.monotonic() - started, 3), dwg=dwg, intermediate_dxf=intermediate_bytes,
        )

    job.check_time("readback")
    libre, rb_sig = _readback_libredwg(job, sig)
    job.check_time("independent readback")
    independent = _readback_ezdwg(job, sig)
    checks = [libre, independent]
    if any(c.status == "failed" for c in checks):
        raise DwgError("dwg_readback_mismatch", "the written DWG does not read back as the source DXF",
                       {"readback": [c.model_dump(mode="json") for c in checks]})
    if independent.status == "skipped":
        warnings.append(Issue(code="dwg_independent_readback_skipped",
                              message=independent.reason or "independent readback skipped"))

    losses = _write_losses(norm, sig, rb_sig)
    return DwgWriteResult(
        target="AC1015",
        source=source,
        intermediate=intermediate,
        output=DwgBlob(format="dwg", role="output", sha256=_sha(dwg), size=len(dwg), version=version),
        runtime=job.runtime,
        steps=job.steps,
        readback=checks,
        verified=bool(checks) and all(c.status == "passed" for c in checks),
        losses=losses,
        warnings=warnings,
        seconds=round(time.monotonic() - started, 3),
        dwg=dwg,
        intermediate_dxf=intermediate_bytes,
    )


#: Escaped characters per TEXT value that LibreDWG's DXF reader keeps (cp1252 + ``\\U+XXXX``: 7 per CJK
#: character; 35 CJK characters = 245 survive, 36 = 252 are cut). Measured with LibreDWG 0.13/0.14.
_CJK_TEXT_LIMIT = 240


def _escaped(c: str) -> bool:
    """True for a character the cp1252 DXF carries only as a ``\\U+XXXX`` escape (CJK etc.; not Ø ° ±)."""
    if ord(c) < 128:
        return False
    try:
        c.encode("cp1252")
        return False
    except UnicodeEncodeError:
        return True


def _non_ascii(text: str) -> bool:
    return any(_escaped(c) for c in text)


def _escaped_len(text: str) -> int:
    return sum(7 if _escaped(c) else 1 for c in text)


def _text_pieces(text: str) -> list[str]:
    """Split a TEXT value so that no piece mixes a double quote with non-ASCII characters (LibreDWG's DXF reader
    drops the non-ASCII side of such a value) and no piece exceeds :data:`_CJK_TEXT_LIMIT` escaped characters
    (the limit cuts plain ASCII text too)."""
    runs: list[str] = []
    for ch in text:
        if runs and _escaped(ch) == _escaped(runs[-1][-1]):
            runs[-1] += ch
        else:
            runs.append(ch)
    pieces: list[str] = []
    cur, cur_quote, cur_wide = "", False, False
    for run in runs:
        wide = _escaped(run[0])
        quote = '"' in run
        if cur and ((wide and cur_quote) or (quote and cur_wide)):
            pieces.append(cur)
            cur, cur_quote, cur_wide = "", False, False
        while run:
            room = _CJK_TEXT_LIMIT - _escaped_len(cur)
            take = 0
            while take < len(run) and _escaped_len(run[: take + 1]) <= room:
                take += 1
            if take == 0:
                pieces.append(cur)
                cur, cur_quote, cur_wide = "", False, False
                continue
            cur += run[:take]
            cur_quote, cur_wide = cur_quote or quote, cur_wide or wide
            run = run[take:]
    if cur:
        pieces.append(cur)
    return pieces


def _advance(text: str, height: float, width_factor: float) -> float:
    """Estimated advance of a string: full-width (CJK) characters one text height, others 0.6 of it."""
    em = sum(1.0 if unicodedata.east_asian_width(c) in ("W", "F") else 0.6 for c in text)
    return em * height * (width_factor or 1.0)


def _carry_cjk_text(job: _Job, doc: Any) -> dict[str, Any]:
    """N0: non-ASCII text LibreDWG's DXF reader cannot carry is restructured before the source signature.

    MTEXT with non-ASCII content is exploded into TEXT lines (ezdxf ``MTextExplode``); a TEXT value that mixes a
    double quote with non-ASCII characters or is longer than :data:`_CJK_TEXT_LIMIT` escaped characters is split
    into consecutive TEXT pieces along its baseline (advance estimated from the text height). The readback then
    compares against the restructured text, and every change is reported as the loss ``dwg_cjk_text_restructured``
    with the original strings. Plain ASCII and short CJK text are untouched, so other drawings convert unchanged.
    """
    from ezdxf.addons import MTextExplode

    t0 = time.monotonic()
    exploded: list[str] = []
    split: list[dict[str, Any]] = []
    for block in doc.blocks:
        targets = [e for e in block if e.dxftype() == "MTEXT" and _non_ascii(_u(e.plain_text()))]
        if targets:
            with MTextExplode(block) as xpl:
                for mtext in targets:
                    exploded.append(_u(mtext.plain_text())[:200])
                    xpl.explode(mtext, destroy=True)
    for block in doc.blocks:
        for text in [e for e in block if e.dxftype() == "TEXT"]:
            value = _u(text.dxf.get("text", ""))  # a cp1252 DXF keeps the \\U+XXXX escapes in the value
            if not ('"' in value and _non_ascii(value)) and _escaped_len(value) <= _CJK_TEXT_LIMIT:
                continue
            pieces = _text_pieces(value)
            if len(pieces) < 2:
                continue
            height = float(text.dxf.get("height", 2.5))
            width = float(text.dxf.get("width", 1.0))
            angle = math.radians(float(text.dxf.get("rotation", 0.0)))
            direction = Vec3(math.cos(angle), math.sin(angle), 0)
            halign, valign = int(text.dxf.get("halign", 0)), int(text.dxf.get("valign", 0))
            total = _advance(value, height, width)
            if halign in (1, 2, 4):  # centre / right / middle: anchor on the alignment point
                anchor = Vec3(text.dxf.get("align_point", text.dxf.insert))
                start = anchor - direction * (total * (1.0 if halign == 2 else 0.5))
            else:
                start = Vec3(text.dxf.insert)
            attribs = text.dxfattribs(drop={"handle", "owner", "text", "insert", "align_point", "halign"})
            offset = 0.0
            for piece in pieces:
                point = start + direction * offset
                new = block.add_text(piece, dxfattribs={**attribs, "halign": 0})
                new.dxf.insert = point
                if valign:
                    new.dxf.align_point = point
                offset += _advance(piece, height, width)
            split.append({"text": value[:200], "pieces": len(pieces)})
            block.delete_entity(text)
    job.add("N0", "transform", "non-ASCII text LibreDWG cannot carry restructured: MTEXT exploded into TEXT lines, "
            "TEXT mixing quotes with CJK or over the length limit split along the baseline", t0,
            mtext_exploded=len(exploded), text_split=len(split))
    return {"cjk_mtext_exploded": exploded, "cjk_text_split": split}


def _normalise(job: _Job, doc: Any) -> dict[str, Any]:
    """N1-N4b on the ezdxf document (W5a §1.2, defects D1-D7 and D10)."""
    t0 = time.monotonic()
    mtext_rotated = text_nudged = 0
    for block in doc.blocks:
        for entity in block:
            rtype = entity.dxftype()
            if rtype == "MTEXT" and entity.dxf.hasattr("rotation"):
                rotation = entity.dxf.rotation
                entity.dxf.discard("rotation")
                entity.dxf.text_direction = Vec3.from_deg_angle(rotation)
                mtext_rotated += 1
            elif rtype == "TEXT" and (entity.dxf.get("halign", 0) or entity.dxf.get("valign", 0)):
                insert = Vec3(entity.dxf.insert)
                align = Vec3(entity.dxf.get("align_point", entity.dxf.insert))
                if insert.isclose(align, abs_tol=1e-9):
                    entity.dxf.insert = align - Vec3(1e-3, 0, 0)
                    text_nudged += 1
    job.add("N1", "transform", "MTEXT rotation (group 50, rejected by the LibreDWG DXF importer) stored as "
            "the text_direction vector", t0, mtext_rotation_to_direction=mtext_rotated)
    job.add("N2", "transform", "aligned TEXT with insert == alignment point: insert moved by -0.001 mm in x "
            "so the alignment point is stored (else LibreDWG reads it back as 0,0)", t0, text_insert_nudged=text_nudged)

    t0 = time.monotonic()
    measured = failed = 0
    for block in doc.blocks:
        for entity in block:
            if entity.dxftype() == "DIMENSION":
                try:
                    entity.dxf.actual_measurement = entity.get_measurement()
                    measured += 1
                except Exception:  # reported; the readback compares measurements
                    failed += 1
    job.add("N2b", "transform", "DIMENSION actual measurement (group 42) written so the DWG carries it",
            t0, written=measured, failed=failed)

    t0 = time.monotonic()
    added = []
    for layer in doc.layers:
        linetype = layer.dxf.get("linetype", "Continuous")
        if linetype not in doc.linetypes:
            doc.linetypes.add(linetype, pattern=[3.0, 2.0, -1.0], description=f"{linetype} __ __ __")
            added.append(linetype)
    job.add("N3", "transform", "linetypes referenced by layers but not defined are added (source repair)",
            t0, linetypes_added=added)

    t0 = time.monotonic()
    removed: dict[str, int] = {}
    for key in ("ACAD_MATERIAL", "ACAD_MLEADERSTYLE", "ACAD_VISUALSTYLE"):
        dictionary = doc.rootdict.get(key)
        if dictionary is None:
            continue
        count = 0
        for name, obj in list(dictionary.items()):
            dictionary.discard(name)
            if hasattr(obj, "dxftype"):
                doc.objects.delete_entity(obj)
                count += 1
        doc.rootdict.discard(key)
        doc.objects.delete_entity(dictionary)
        removed[key] = count
    for layer in doc.layers:
        layer.dxf.discard("material_handle")
    job.add("N4", "transform", "R2007+ MATERIAL / MLEADERSTYLE / VISUALSTYLE objects removed (LibreDWG "
            "encodes them wrongly for R2000; they do not exist in R2000)", t0, objects_removed=removed)

    t0 = time.monotonic()
    color = doc.rootdict.get("ACAD_COLOR")
    color_removed = color is not None and len(color) == 0 and color.dxf.handle == "B"
    if color_removed:
        doc.rootdict.discard("ACAD_COLOR")
        doc.objects.delete_entity(color)
    job.add("N4b", "transform", "empty ACAD_COLOR dictionary at handle B removed (LibreDWG creates "
            "VX_CONTROL at the fixed handle B)", t0, removed=color_removed)
    return {"mtext_rotation_to_direction": mtext_rotated, "text_insert_nudged": text_nudged,
            "linetypes_added": added, "objects_removed": removed, "acad_color_removed": color_removed}


_HANDLE_CODES = {5, 105, 1005, 480, 481} | set(range(320, 370)) | set(range(390, 400))


def _save_r2000(job: _Job, doc: Any) -> bytes:
    """N6 (save as DXF R2000), then the tag-level fixes N5 and N7."""
    t0 = time.monotonic()
    doc.dxfversion = "R2000"
    path = job.workdir / "normalised.dxf"
    # ezdxf's documented switch for fixed $TDCREATE/$TDUPDATE and GUIDs, so the same DXF always gives
    # the same DWG bytes; process-global, so set and restored under the shared lock (dxf_write.py).
    with fixed_metadata():
        doc.saveas(path)
    encoding = doc.encoding
    job.add("N6", "transform", "saved as DXF R2000: cp1252 plus \\U+XXXX escapes, the pre-2007 form the "
            "R2000 writer expects (UTF-8 input becomes mojibake)", t0, dxfversion=doc.dxfversion, encoding=encoding)
    try:
        lines = path.read_bytes().decode(encoding).split("\n")
    except UnicodeDecodeError as exc:
        raise DwgError("dwg_conversion_failed", f"normalised DXF is not valid {encoding}") from exc
    pairs = [(lines[k], lines[k + 1]) for k in range(0, len(lines) - 1, 2)]
    tail = lines[len(pairs) * 2:]

    t0 = time.monotonic()
    pairs, moved = _move_dimension_rotation(pairs)
    job.add("N5", "transform", "DIMENSION group 50 moved behind the AcDbRotatedDimension marker (else the "
            "importer stores it as the oblique angle and vertical dimensions measure 0)", t0, moved=moved)
    t0 = time.monotonic()
    pairs, renumbered = _renumber_model_space(pairs)
    job.add("N7", "transform", "model-space BLOCK_RECORD handle renumbered to 1F, the handle LibreDWG "
            "pre-creates for *Model_Space", t0, renumbered=renumbered)
    text = "\n".join(f"{code}\n{value}" for code, value in pairs) + ("\n" + "\n".join(tail) if tail else "")
    return text.encode(encoding)


def _records_of(pairs: list[tuple[str, str]]) -> list[tuple[int, int]]:
    starts = [k for k, (code, _) in enumerate(pairs) if code.strip() == "0"]
    return [(s, starts[n + 1] if n + 1 < len(starts) else len(pairs)) for n, s in enumerate(starts)]


def _move_dimension_rotation(pairs: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], int]:
    out: list[tuple[str, str]] = []
    moved = 0
    prev = 0
    for start, end in _records_of(pairs):
        out.extend(pairs[prev:start])
        record = pairs[start:end]
        prev = end
        if record[0][1].strip() == "DIMENSION":
            marker = next((k for k, p in enumerate(record) if p[1].strip() == "AcDbRotatedDimension"), None)
            angles = [(k, p) for k, p in enumerate(record) if p[0].strip() == "50"]
            if marker is not None and angles and any(k < marker for k, _ in angles):
                rest = [p for p in record if p[0].strip() != "50"]
                marker = next(k for k, p in enumerate(rest) if p[1].strip() == "AcDbRotatedDimension")
                record = rest[:marker + 1] + [p for _, p in angles] + rest[marker + 1:]
                moved += 1
        out.extend(record)
    out.extend(pairs[prev:])
    return out, moved


def _renumber_model_space(pairs: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], str | None]:
    model_space = None
    used: set[str] = set()
    for code, value in pairs:
        if code.strip() in ("5", "105"):
            used.add(value.strip().upper())
    for start, end in _records_of(pairs):
        if pairs[start][1].strip() != "BLOCK_RECORD":
            continue
        record = pairs[start:end]
        name = next((v.strip() for c, v in record if c.strip() == "2"), "")
        if name.upper() == "*MODEL_SPACE":
            model_space = next((v.strip().upper() for c, v in record if c.strip() == "5"), None)
            break
    if model_space is None:
        raise DwgError("dwg_conversion_failed", "normalised DXF has no *Model_Space block record")
    if model_space == "1F":
        return pairs, None
    if "1F" in used:
        raise DwgError("dwg_conversion_failed",
                       "handle 1F is taken, LibreDWG would create a second *Model_Space record (D7)")
    out = []
    for code, value in pairs:
        stripped = code.strip()
        if stripped.lstrip("-").isdigit() and int(stripped) in _HANDLE_CODES and value.strip().upper() == model_space:
            value = value.replace(value.strip(), "1F")
        out.append((code, value))
    return out, f"{model_space}->1F"


class _JsonObject(list):
    """JSON object as ordered (key, value) pairs: keeps LibreDWG's repeated
    keys (``_subclass``) and field order through a load/dump round trip."""

    def get(self, key: str, default: Any = None) -> Any:
        for k, v in self:
            if k == key:
                return v
        return default

    def has(self, key: str) -> bool:
        return any(k == key for k, _ in self)

    def set(self, key: str, value: Any) -> None:
        for n, (k, _) in enumerate(self):
            if k == key:
                self[n] = (k, value)
                return
        self.append((key, value))


def _dump_json(value: Any) -> str:
    if isinstance(value, _JsonObject):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _dump_json(v) for k, v in value) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_dump_json(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


_UNIT_PREAMBLE = {"unit1_ratio": 412148564080.0, "unit2_ratio": 1.0, "unit3_ratio": 1.0, "unit4_ratio": 1.0,
                  "unit1_name": "m"}


def _patch_json(job: _Job, raw: bytes) -> bytes:
    """N8: header preamble (unit ratios / name) as AutoCAD writes it, so other
    readers find the header variables. N8b: TEXT-like width factor 1.0 where
    the JSON left it out (dwgwrite would zero-fill it)."""
    t0 = time.monotonic()
    _, nonfinite = _tolerant_json(raw)
    if nonfinite:
        raise DwgError("dwg_conversion_failed",
                       f"LibreDWG JSON of the converted DWG holds {nonfinite} NaN/Inf value(s)")
    try:
        model = json.loads(raw.decode("utf-8"), object_pairs_hook=_JsonObject)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DwgError("dwg_conversion_failed", f"LibreDWG JSON is not parseable: {exc}") from exc
    header = model.get("HEADER")
    objects = model.get("OBJECTS")
    if not isinstance(header, _JsonObject) or not isinstance(objects, list):
        raise DwgError("dwg_conversion_failed", "LibreDWG JSON lacks HEADER or OBJECTS")
    before = {k: header.get(k) for k in _UNIT_PREAMBLE}
    for key, value in _UNIT_PREAMBLE.items():
        header.set(key, value)
    job.add("N8", "transform", "DWG header unit preamble set to AutoCAD's values (LibreDWG writes 0/'', "
            "independent readers then cannot locate the header variables)", t0, before=before, after=_UNIT_PREAMBLE)
    t0 = time.monotonic()
    restored = 0
    for obj in objects:
        if isinstance(obj, _JsonObject) and obj.get("entity") in ("TEXT", "ATTRIB", "ATTDEF") \
                and not obj.has("width_factor"):
            obj.set("width_factor", 1.0)
            restored += 1
    job.add("N8b", "transform", "TEXT/ATTRIB/ATTDEF width factor 1.0 restored where the JSON omitted the "
            "default", t0, width_factor_restored=restored)
    return _dump_json(model).encode("utf-8")


# --------------------------------------------------------------------------- #
# Signatures for the readback comparison
# --------------------------------------------------------------------------- #

_U_ESCAPE = re.compile(r"\\U\+([0-9A-Fa-f]{4})")
_EZDWG_DIMTYPE = {"LINEAR": 0, "ALIGNED": 1, "ANG2LN": 2, "DIAMETER": 3, "RADIUS": 4, "ANG3PT": 5, "ORDINATE": 6}
# Dimension-style variables with no R2000 field: their loss is expected.
_R2007_DIMVARS = {"dimfxl", "dimfxlon", "dimjogang", "dimtfill", "dimtfillclr", "dimarcsym", "dimltype",
                  "dimltex1", "dimltex2", "dimtxtdirection", "dimmzf", "dimmzs", "dimaltmzf", "dimaltmzs"}
_DIMVARS = tuple(k for k in acdb_dimstyle.attribs if k.startswith("dim") and not k.endswith("_handle"))
_HEADER_VARS = ("$INSUNITS", "$MEASUREMENT", "$LUNITS", "$LUPREC", "$AUNITS", "$DIMSTYLE", "$TEXTSTYLE",
                "$DIMBLK", "$DIMSCALE", "$DIMASZ", "$DIMTXT", "$LTSCALE", "$CLAYER")


def _u(text: Any) -> str:
    return _U_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text) if isinstance(text, str) else ""


def _r(value: Any) -> float:
    # Two steps: text round trips differ in the last bits (0.018750000000000003
    # vs 0.01875), which must not decide a 4-decimal rounding boundary.
    return round(round(float(value), 9), 4) + 0.0


def _p(point: Any) -> tuple[float, float]:
    seq = tuple(point)
    return _r(seq[0]), _r(seq[1])


def _a(angle: Any) -> float:
    return round(float(angle) % 360.0, 4) % 360.0 + 0.0


def _measure(entity: Any) -> float | str:
    try:
        return _r(entity.get_measurement())
    except Exception as exc:  # compared as text on both sides
        return f"error: {type(exc).__name__}"


def _ezdxf_sig(entity: Any, *, full: bool) -> tuple[Any, ...]:
    """Signature of an ezdxf entity. ``full`` adds fields only ezdxf exposes on
    both sides (LibreDWG readback); the short form matches :func:`_ezdwg_sig`."""
    rtype, dxf = entity.dxftype(), entity.dxf
    layer = dxf.layer
    if rtype == "LINE":
        return rtype, layer, tuple(sorted([_p(dxf.start), _p(dxf.end)]))
    if rtype == "LWPOLYLINE":
        points = tuple((_r(x), _r(y), _r(b)) for x, y, b in entity.get_points("xyb"))
        return rtype, layer, points, bool(entity.closed)
    if rtype == "CIRCLE":
        return rtype, layer, _p(dxf.center), _r(dxf.radius)
    if rtype == "ARC":
        return rtype, layer, _p(dxf.center), _r(dxf.radius), _a(dxf.start_angle), _a(dxf.end_angle)
    if rtype == "SOLID":
        return rtype, layer, tuple(_p(dxf.get(f"vtx{i}", (0, 0))) for i in range(4))
    if rtype == "POINT":
        return rtype, layer, _p(dxf.location)
    if rtype == "TEXT":
        halign, valign = dxf.get("halign", 0), dxf.get("valign", 0)
        anchor = _p(dxf.get("align_point", dxf.insert)) if (halign or valign) else _p(dxf.insert)
        sig = (rtype, layer, _u(dxf.text), _r(dxf.height), _a(dxf.get("rotation", 0)), anchor, halign, valign,
               _r(dxf.get("width", 1)), _r(dxf.get("oblique", 0)))
        return sig + ((dxf.get("style", "Standard") or "Standard").upper(),) if full else sig
    if rtype == "MTEXT":
        return rtype, layer, _u(entity.text), _p(dxf.insert)
    if rtype == "INSERT":
        return (rtype, layer, dxf.name.upper(), _p(dxf.insert), _a(dxf.get("rotation", 0)),
                _r(dxf.get("xscale", 1)), _r(dxf.get("yscale", 1)))
    if rtype == "DIMENSION":
        sig = (rtype, layer, entity.dimtype, _u(dxf.get("text", "")), _measure(entity), _p(dxf.defpoint),
               _p(dxf.get("defpoint2", (0, 0))), _p(dxf.get("defpoint3", (0, 0))), _a(dxf.get("angle", 0)))
        return sig + (dxf.get("dimstyle", "").upper(),) if full else sig
    return rtype, layer


def _ezdwg_sig(entity: Any, layer_names: dict[int, str]) -> tuple[Any, ...]:
    rtype, dxf = entity.dxftype, entity.dxf
    layer = layer_names.get(dxf.get("layer_handle"), "?")
    if rtype == "LINE":
        return rtype, layer, tuple(sorted([_p(dxf["start"]), _p(dxf["end"])]))
    if rtype == "LWPOLYLINE":
        points = list(dxf.get("points") or [])
        bulges = list(dxf.get("bulges") or [])
        bulges += [0.0] * (len(points) - len(bulges))
        return rtype, layer, tuple((_r(p[0]), _r(p[1]), _r(b)) for p, b in zip(points, bulges)), \
            bool(dxf.get("closed"))
    if rtype == "CIRCLE":
        return rtype, layer, _p(dxf["center"]), _r(dxf["radius"])
    if rtype == "ARC":
        return rtype, layer, _p(dxf["center"]), _r(dxf["radius"]), _a(dxf["start_angle"]), _a(dxf["end_angle"])
    if rtype == "SOLID":
        return rtype, layer, tuple(_p(p) for p in list(dxf["points"])[:4])
    if rtype == "POINT":
        return rtype, layer, _p(dxf["location"])
    if rtype == "TEXT":
        halign, valign = dxf.get("halign", 0) or 0, dxf.get("valign", 0) or 0
        anchor = _p(dxf["align_point"]) if (halign or valign) else _p(dxf["insert"])
        return (rtype, layer, _u(dxf.get("text")), _r(dxf["height"]), _a(dxf.get("rotation", 0)), anchor,
                halign, valign, _r(dxf.get("width", 1)), _r(dxf.get("oblique", 0)))
    if rtype == "MTEXT":
        return rtype, layer, _u(dxf.get("raw_text", dxf.get("text"))), _p(dxf["insert"])
    if rtype == "INSERT":
        return (rtype, layer, str(dxf.get("name", "")).upper(), _p(dxf["insert"]), _a(dxf.get("rotation", 0)),
                _r(dxf.get("xscale", 1)), _r(dxf.get("yscale", 1)))
    if rtype == "DIMENSION":
        measured = dxf.get("actual_measurement")
        return (rtype, layer, _EZDWG_DIMTYPE.get(str(dxf.get("dimtype", "")).upper(), -1), _u(dxf.get("text", "")),
                _r(measured) if measured is not None else "missing", _p(dxf["defpoint"]), _p(dxf["defpoint2"]),
                _p(dxf["defpoint3"]), _a(dxf.get("angle", 0)))
    return rtype, layer


def _dimension_detail(doc: Any, entity: Any) -> str:
    """Canonical JSON of everything compared for one DIMENSION (full readback)."""
    dxf = entity.dxf
    try:
        override = {k: (_r(v) if isinstance(v, float) else v)
                    for k, v in sorted(entity.override().dimstyle_attribs.items())}
    except Exception as exc:  # compared as text on both sides
        override = {"error": type(exc).__name__}
    name = dxf.get("geometry")
    block = doc.blocks.get(name) if name else None
    detail = {
        "dimtype": entity.dimtype,
        "text": _u(dxf.get("text", "")),
        "dimstyle": dxf.get("dimstyle", "").upper(),
        "defpoints": [_p(dxf.defpoint), _p(dxf.get("defpoint2", (0, 0))), _p(dxf.get("defpoint3", (0, 0)))],
        "text_midpoint": _p(dxf.get("text_midpoint", (0, 0))),
        "angle": _a(dxf.get("angle", 0)),
        "measurement": _measure(entity),
        "override": override,
        "block": None if block is None else dict(sorted(collections.Counter(e.dxftype() for e in block).items())),
        "block_texts": None if block is None else sorted(
            _u(e.dxf.text if e.dxftype() == "TEXT" else e.plain_text()) for e in block
            if e.dxftype() in ("TEXT", "MTEXT")),
    }
    return json.dumps(detail, sort_keys=True, default=str, ensure_ascii=False)


@dataclass
class _Signature:
    full: collections.Counter[tuple[Any, ...]]
    short: collections.Counter[tuple[Any, ...]]
    counts: collections.Counter[tuple[str, str]]
    dims: list[str]
    stored_measurements: list[Any]
    texts: list[str]
    layers: dict[str, tuple[Any, ...]]
    linetypes: set[str]
    styles: dict[str, str]
    dimstyles: dict[str, dict[str, Any]]
    layouts: list[str]
    header: dict[str, Any]


def _dimvar(style: Any, name: str) -> Any:
    """Dimension-style variable with the DXF default applied (LibreDWG omits defaults)."""
    value = style.dxf.get(name)
    if value is None:
        try:
            value = style.dxf.get_default(name)
        except Exception:  # no default defined
            value = None
    return _r(value) if isinstance(value, float) else value


def _signature(doc: Any) -> _Signature:
    msp = doc.modelspace()
    entities = list(msp)
    dims = [e for e in entities if e.dxftype() == "DIMENSION"]
    dimstyles = {style.dxf.name.upper(): {k: _dimvar(style, k) for k in _DIMVARS} for style in doc.dimstyles}
    return _Signature(
        full=collections.Counter(_ezdxf_sig(e, full=True) for e in entities),
        short=collections.Counter(_ezdxf_sig(e, full=False) for e in entities),
        counts=collections.Counter((e.dxf.layer, e.dxftype()) for e in entities),
        dims=sorted(_dimension_detail(doc, e) for e in dims),
        stored_measurements=list((_r(e.dxf.actual_measurement) if e.dxf.hasattr("actual_measurement")
                                    else "missing") for e in dims),
        texts=sorted(_u(e.dxf.text) for e in entities if e.dxftype() == "TEXT")
        + sorted(_u(e.plain_text()) for e in entities if e.dxftype() == "MTEXT"),
        layers={layer.dxf.name.upper(): (layer.dxf.get("color", 7), layer.dxf.get("linetype", "Continuous").upper(),
                                         layer.dxf.get("lineweight", -3)) for layer in doc.layers},
        linetypes={lt.dxf.name.upper() for lt in doc.linetypes},
        styles={s.dxf.name.upper(): s.dxf.get("font", "") for s in doc.styles},
        dimstyles=dimstyles,
        layouts=sorted(layout.name.upper() for layout in doc.layouts),
        header={name: doc.header.get(name) for name in _HEADER_VARS},
    )


def _fmt(key: Any) -> str:
    return str(key)[:240]


def _dict_diff(expected: dict[str, Any], actual: dict[str, Any]) -> str:
    keys = sorted(k for k in set(expected) | set(actual) if expected.get(k) != actual.get(k))
    return _fmt({k: (expected.get(k), actual.get(k)) for k in keys})


def _compare(label: str, expected: collections.Counter[Any], actual: collections.Counter[Any],
             mismatches: list[str]) -> int:
    missing, extra = expected - actual, actual - expected
    for key in list(missing.elements())[:10]:
        mismatches.append(f"{label} missing: {_fmt(key)}")
    for key in list(extra.elements())[:10]:
        mismatches.append(f"{label} unexpected: {_fmt(key)}")
    return sum((expected & actual).values())


def _readback_libredwg(job: _Job, sig: _Signature) -> tuple[DwgReadbackCheck, _Signature | None]:
    t0 = time.monotonic()
    check = DwgReadbackCheck(reader="libredwg", reader_version=job.runtime.libredwg_version, status="failed",
                             entities=sum(sig.full.values()), dimensions=len(sig.dims), texts=len(sig.texts))
    step, errors = job.run("dwg2dxf", ["-y", "-o", "readback.dxf", "output.dwg"],
                           "readback: written DWG -> DXF (LibreDWG dwg2dxf)")
    path = job.workdir / "readback.dxf"
    if step.exit_code != 0 or errors or not path.is_file():
        check.reason = f"dwg2dxf readback: exit {step.exit_code}, {len(errors)} error(s)"
        check.mismatches = list(_top(errors, 10))
        check.seconds = round(time.monotonic() - t0, 3)
        return check, None
    try:
        doc = ezdxf.readfile(path)
    except Exception as exc:  # strict only: needing recover is a failure here
        check.reason = f"readback DXF does not load strictly: {type(exc).__name__}: {str(exc)[:200]}"
        check.seconds = round(time.monotonic() - t0, 3)
        return check, None
    rb = _signature(doc)
    mismatches: list[str] = []
    check.entities_matched = _compare("entity", sig.full, rb.full, mismatches)
    _compare("layer/type count", sig.counts, rb.counts, mismatches)
    check.dimensions_matched = _compare("dimension", collections.Counter(sig.dims), collections.Counter(rb.dims),
                                        mismatches)
    expected_stored = sorted((json.loads(d)["measurement"] for d in sig.dims), key=str)
    if sorted(rb.stored_measurements, key=str) != expected_stored:
        mismatches.append(f"stored dimension measurements {rb.stored_measurements} != {expected_stored}")
    _compare("text", collections.Counter(sig.texts), collections.Counter(rb.texts), mismatches)
    for name in ("$INSUNITS", "$MEASUREMENT"):
        if sig.header.get(name) != rb.header.get(name):
            mismatches.append(f"header {name}: {sig.header.get(name)} != {rb.header.get(name)}")
    if sig.layers != rb.layers:
        mismatches.append(f"layers differ: {_dict_diff(sig.layers, rb.layers)}")
    lost_linetypes = sig.linetypes - rb.linetypes
    if lost_linetypes:
        mismatches.append(f"linetypes lost: {sorted(lost_linetypes)}")
    if sig.styles != rb.styles:
        mismatches.append(f"text styles differ: {_dict_diff(sig.styles, rb.styles)}")
    if set(sig.dimstyles) != set(rb.dimstyles):
        mismatches.append(f"dimension styles differ: {sorted(set(sig.dimstyles) ^ set(rb.dimstyles))}")
    # dwg2dxf leaves out values equal to LibreDWG's defaults, so style values are
    # compared with the DWG object model itself (dwgread JSON of the output).
    stored = _dwg_dimstyles(job)
    for name, attribs in sig.dimstyles.items():
        values = stored.get(name)
        if values is None:
            mismatches.append(f"dimension style {name} missing in the DWG object model")
            continue
        changed = sorted(k for k, v in attribs.items()
                         if k in values and k not in _R2007_DIMVARS and v != values[k])
        if changed:
            mismatches.append(f"dimension style {name} changed: "
                              f"{_fmt({k: (attribs[k], values[k]) for k in changed})}")
    if sig.layouts != rb.layouts:
        mismatches.append(f"layouts {sig.layouts} != {rb.layouts}")
    check.mismatches = mismatches[:40]
    check.status = "passed" if not mismatches else "failed"
    check.reason = None if not mismatches else f"{len(mismatches)} difference(s) from the source DXF"
    check.seconds = round(time.monotonic() - t0, 3)
    return check, rb


def _dwg_dimstyles(job: _Job) -> dict[str, dict[str, Any]]:
    """Scalar DIMSTYLE fields of the written DWG (dwgread JSON), by style name."""
    step, errors = job.run("dwgread", ["-O", "JSON", "-o", "readback.json", "output.dwg"],
                           "readback: written DWG -> JSON object model, for dimension-style values")
    raw = job.require_output(step, errors, job.workdir / "readback.json", strict=True, max_bytes=MAX_JSON_OUTPUT_BYTES)
    fixed, nonfinite = _tolerant_json(raw)
    if nonfinite:
        raise DwgError("dwg_readback_mismatch", f"written DWG decodes with {nonfinite} NaN/Inf value(s)")
    styles: dict[str, dict[str, Any]] = {}
    for obj in json.loads(fixed.decode("utf-8", "replace")).get("OBJECTS", []):
        if isinstance(obj, dict) and obj.get("object") == "DIMSTYLE" and isinstance(obj.get("name"), str):
            styles[obj["name"].upper()] = {
                k.lower(): (_r(v) if isinstance(v, float) else v) for k, v in obj.items()
                if k.startswith("DIM") and isinstance(v, (int, float, str)) and not isinstance(v, bool)}
    return styles


def _readback_ezdwg(job: _Job, sig: _Signature) -> DwgReadbackCheck:
    reader = job.runtime.independent_reader
    check = DwgReadbackCheck(reader="ezdwg", reader_version=reader.split(" ", 1)[-1] if reader else None,
                             status="skipped", entities=sum(sig.short.values()), dimensions=len(sig.dims),
                             texts=len(sig.texts))
    if not reader:
        check.reason = "independent reader ezdwg is not installed on this host"
        return check
    t0 = time.monotonic()
    try:
        import ezdwg
    except ImportError as exc:  # installed but broken: fail closed
        check.status = "failed"
        check.reason = f"ezdwg cannot be imported: {exc}"
        return check
    try:
        doc = ezdwg.read(str(job.workdir / "output.dwg"))
        layer_names = {v["handle"]: k for k, v in doc.layers().items()}
        entities = [e for e in doc.entities().query() if (doc.entity_placement(e.handle) or (None,))[0] == 2]
        actual = collections.Counter(_ezdwg_sig(e, layer_names) for e in entities)
        mismatches: list[str] = []
        check.entities_matched = _compare("entity", sig.short, actual, mismatches)
        expected_dims = collections.Counter(k for k in sig.short.elements() if k[0] == "DIMENSION")
        actual_dims = collections.Counter(k for k in actual.elements() if k[0] == "DIMENSION")
        check.dimensions_matched = sum((expected_dims & actual_dims).values())
        texts = sorted(_u(e.dxf.get("text")) for e in entities if e.dxftype == "TEXT") + sorted(
            _u(e.dxf.get("text")) for e in entities if e.dxftype == "MTEXT")
        if texts != sig.texts:
            mismatches.append("text strings differ")
        layers = {k.upper(): (v["color_index"], str(v["linetype"]).upper(), v["lineweight"])
                  for k, v in doc.layers().items()}
        if layers != sig.layers:
            mismatches.append(f"layers differ: {_dict_diff(sig.layers, layers)}")
        insunits = doc.header_variables().get("insunits")
        if insunits != sig.header.get("$INSUNITS"):
            mismatches.append(f"header $INSUNITS: {sig.header.get('$INSUNITS')} != {insunits}")
        check.mismatches = mismatches[:40]
        check.status = "passed" if not mismatches else "failed"
        check.reason = None if not mismatches else f"{len(mismatches)} difference(s) from the source DXF"
    except Exception as exc:  # any reader failure fails the check (fail closed)
        check.status = "failed"
        check.reason = f"ezdwg could not read the DWG: {type(exc).__name__}: {str(exc)[:200]}"
    finally:
        ezdwg.clear_decode_caches()
    check.seconds = round(time.monotonic() - t0, 3)
    job.add("readback_ezdwg", "check", "independent decode of the written DWG (ezdwg, MIT)", t0,
            status=check.status, matched=check.entities_matched, entities=check.entities)
    return check


def _write_losses(norm: dict[str, Any], sig: _Signature, rb: _Signature | None) -> list[Issue]:
    losses: list[Issue] = []
    if norm["objects_removed"]:
        losses.append(Issue(code="dwg_objects_removed",
                            message="R2007+ objects with no R2000 equivalent were removed before conversion",
                            details=norm["objects_removed"]))
    if norm["acad_color_removed"]:
        losses.append(Issue(code="dwg_empty_dictionary_removed", message="empty ACAD_COLOR dictionary removed"))
    if norm["text_insert_nudged"]:
        losses.append(Issue(code="dwg_text_insert_nudged",
                            message="aligned TEXT insert points moved by 0.001 mm (alignment points unchanged)",
                            details={"count": norm["text_insert_nudged"]}))
    if norm.get("cjk_mtext_exploded") or norm.get("cjk_text_split"):
        losses.append(Issue(code="dwg_cjk_text_restructured",
                            message="non-ASCII text LibreDWG cannot carry was restructured (N0): multi-line text exploded "
                                    "into single lines, long or quoted lines split along the baseline",
                            details={"mtext_exploded": norm.get("cjk_mtext_exploded") or [],
                                     "text_split": norm.get("cjk_text_split") or []}))
    losses.append(Issue(code="dwg_handles_changed",
                        message="model-space block record handle and $HANDSEED change in the DWG"))
    if rb is not None:
        changed = {name: [sig.header.get(name), rb.header.get(name)] for name in _HEADER_VARS
                   if sig.header.get(name) != rb.header.get(name)}
        if changed:
            losses.append(Issue(code="dwg_header_variables_changed",
                                message="header variables that differ after the DWG round trip",
                                details={k: [str(a), str(b)] for k, (a, b) in changed.items()}))
        dropped = {}
        for name, attribs in sig.dimstyles.items():
            other = rb.dimstyles.get(name, {})
            keys = sorted(k for k in _R2007_DIMVARS if attribs.get(k) != other.get(k))
            if keys:
                dropped[name] = keys
        if dropped:
            losses.append(Issue(code="dwg_dimstyle_variables_dropped",
                                message="dimension-style variables R2000 has no field for", details=dropped))
    return losses
