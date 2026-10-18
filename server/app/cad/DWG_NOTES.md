# Native DWG adapter (`app/cad/dwg.py`)

CAD-layer adapter for native DWG input and output (work package W5b). The
evidence behind every step is the W5a report:
`.trellis/tasks/archive/2026-10/10-08-cad-ai/research/w5a-dwg-feasibility.md`. No service, API
or workflow uses this module yet; that wiring belongs to the drawing-package
work.

Public API: `dwg_runtime()`, `detect_dwg_version()`, `require_dwg()`,
`read_dwg()`, `write_dwg()` and `DwgError`. The result models (`DwgRuntime`,
`DwgReadResult`, `DwgWriteResult`, `DwgStep`, …) are in `results.py`. They
dump to JSON with `model_dump(mode="json")`, so they can be stored as
provenance. File bytes (`dxf`, `dwg`, `intermediate_dxf`) are left out of
every dump.

## Runtime

- **Converter.** GNU LibreDWG 0.14 provides four command-line tools:
  `dwg2dxf`, `dxf2dwg`, `dwgread` and `dwgwrite`.
  - The server image builds them in `server/Dockerfile` from the official GNU
    tarball, checked against sha256 `cb6ee0b0…5218351`, and installs them in
    `/opt/libredwg`.
  - For local development, use Homebrew `libredwg` or any 0.14 build on
    `PATH`. On Apple silicon the Homebrew build is x86_64 and runs under
    Rosetta.
  - `PARTROUTE_LIBREDWG_BIN_DIR` points to one specific directory. When it is
    set, `PATH` is not searched.
- **`dwg_runtime(bin_dir)`.** This call never raises. It reports:
  - the path, version and sha256 of each tool;
  - the sha256 of the shared library;
  - a 16-hex build fingerprint;
  - the host, the ezdxf version and the independent reader.

  Only LibreDWG 0.14 is accepted (`SUPPORTED_LIBREDWG_VERSIONS`). The
  normalisation recipe is release-specific, so any other release reports
  `available=False` until it is verified again.
- **Independent reader.** ezdwg 0.12.12 (MIT, Rust core) is pinned in
  `pyproject.toml` with a platform marker.
  - PyPI has wheels only for macOS (universal2) and Linux x86_64. There is
    **no Linux aarch64 wheel**, so arm64 images (for example a local Docker
    build on Apple silicon) do not have it. Cloud Run (linux/amd64) does.
  - Without ezdwg, `write_dwg` raises `dwg_runtime_unavailable`. If you call
    it with `require_independent=False`, the ezdwg check becomes `skipped` and
    `verified` is False. A skipped check is never reported as `passed`.
- **Process hygiene.** DWG input is untrusted: LibreDWG's NEWS lists repeated
  decoder out-of-bounds fixes, and W5a S6 saw an over-read.
  - Every call runs in a fresh temporary directory, with fixed file names and
    a minimal `LC_ALL=C` environment.
  - `timeout_s` (`PARTROUTE_DWG_TIMEOUT_S`, default 120 s) is the wall-clock
    budget for the whole read or write. When it runs out, the tool's process
    group is killed and the call raises `dwg_timeout`.
  - On Linux, the tools also get best-effort `prlimit` limits: 4 GiB address
    space, 2 GiB file size and no core dumps.
- **Fonts.** The image installs `fonts-dejavu-core` and `fonts-wqy-microhei`
  so that ezdxf text extents work. The slim base has no fonts at all (W5a
  S8).

## Declared versions

| Direction | Versions | Status |
| --- | --- | --- |
| Write | AC1015 (R2000) | The only target. Any other target raises `dwg_version_unsupported`. |
| Read | AC1015, AC1018, AC1024, AC1027, AC1032 | **Provisional** |
| Read | AC1021 (R2007) | Accepted but **untested**: no real file has been tried. |
| Read | Anything else | Raises `dwg_version_unsupported`. |

Why only AC1015 for write (W5a §1.4):

- r14 output fails readback.
- r2004 output is 28× larger and logs handle warnings.
- r2010 and later can only be written by `dwgwrite`, and LibreDWG documents CRC
  errors for those versions.

"Provisional" means the version is not declared supported yet. Acceptance §5
first needs at least 2 external-author DWGs and an independent CAD editor
check. Read results carry `version_status` and a `dwg_version_*` warning.

## Errors (`DwgError.code`)

| Code | When |
| --- | --- |
| `dwg_runtime_unavailable` | A tool is missing, the LibreDWG release is not verified, or ezdwg is missing while `require_independent=True`. |
| `dwg_not_dwg` | The bytes are not DWG: DXF text, binary DXF or anything else. Only the content counts, never the file name. |
| `dwg_version_unsupported` | The read version or the write target is outside the declaration above. |
| `dwg_conversion_failed` | Any of these: the file is shorter than its header declares; a tool exits non-zero; a structural decoder error; output is missing or incomplete; control bytes persist; the derived DXF does not load; the source DXF does not load. |
| `dwg_readback_mismatch` | The written DWG does not read back as the source DXF in one of the readers. |
| `dwg_timeout` | The time budget ran out. |

Once a job has started, `details` holds the source identity and every step
that ran. That is the failure evidence. Nothing partial is ever returned.

## Write path: DXF → DWG R2000

Exit code 0 and the `AC1015` magic are recorded in the `container` step. They
are never the success criterion. The DXF intermediate is an interim artefact
(`role="intermediate"`), not a deliverable. A DXF is never renamed to DWG.

| Step | What it does | Why (W5a defect) |
| --- | --- | --- |
| N0 | Restructures non-ASCII text LibreDWG cannot carry: MTEXT with Chinese becomes TEXT lines (ezdxf `MTextExplode`); a TEXT value mixing a double quote with Chinese, or longer than 240 escaped characters (35 Chinese characters), is split into consecutive TEXT pieces along its baseline (advance estimated from the text height). Runs before the source signature; every change is the reported loss `dwg_cjk_text_restructured` with the original strings. | Measured 2026-10-10: MTEXT with Chinese reads back garbled; the Chinese part before or after a `"` is dropped; a value over ~250 escaped characters is cut. |
| N1 | Converts MTEXT rotation (group 50) to the `text_direction` vector. | D1: the importer rejects MTEXT group 50, so there is no DWG at all. |
| N2 | Moves the insert point of aligned TEXT by −0.001 mm in x when it equals the alignment point. | D3: otherwise the alignment point is not stored and reads back as (0,0). |
| N2b | Writes the DIMENSION actual measurement (group 42). | So the DWG carries the measured value. |
| N3 | Adds linetypes that layers reference but the file does not define. This is a source repair, reported as a warning. | D10: `drawing.py` layer HIDDEN refers to an undefined linetype. W4 should fix this at the source. |
| N4 | Removes the R2007+ MATERIAL, MLEADERSTYLE and VISUALSTYLE objects. | D5: LibreDWG encodes them wrongly for R2000, and R2000 has no such objects. |
| N4b | Removes an empty `ACAD_COLOR` dictionary at handle B. | D6: LibreDWG creates VX_CONTROL at the fixed handle B. |
| N6 | Saves as DXF R2000: cp1252 with `\U+XXXX` escapes, and fixed metadata so the output is deterministic. | D4: UTF-8 input becomes mojibake. |
| N5 | Moves DIMENSION group 50 behind the `AcDbRotatedDimension` marker. | D2: otherwise group 50 is stored as the oblique angle and vertical dimensions measure 0. |
| N7 | Renumbers the model-space BLOCK_RECORD to handle 1F. Fails if 1F is already taken. | D7: avoids a duplicate `*Model_Space` record. |
| — | Runs `dxf2dwg --as r2000`. Any ERROR line fails. | |
| N8 | Through `dwgread -O JSON`, sets the header unit preamble to AutoCAD's values. | D8: other readers otherwise cannot find the header variables. |
| N8b | Restores TEXT/ATTRIB/ATTDEF `width_factor = 1.0` where the JSON left it out. | D9: `dwgwrite` zero-fills missing fields. |
| — | Runs `dwgwrite -I JSON --as r2000`. The JSON keeps LibreDWG's repeated keys and field order. | |

After writing, two readbacks are compared with signatures of the **source**
DXF. The signatures are taken before normalisation, after N0 (whose changes are reported losses).

- **LibreDWG readback.** `dwg2dxf` must produce 0 ERROR lines, and the result
  must load in ezdxf strict mode. The comparison covers:
  - every model-space entity: type, layer, coordinates to 4 dp, text, height,
    rotation, alignment point, style, width and oblique;
  - per-layer type counts;
  - every DIMENSION: type, defpoints, text midpoint, angle, text, measurement,
    stored measurement, DSTYLE overrides, and the content and texts of its
    geometry block;
  - text strings;
  - `$INSUNITS` and `$MEASUREMENT`;
  - layers (colour, linetype, lineweight), linetypes, text styles with their
    fonts, dimension-style names and layouts.

  Dimension-style values are compared with `dwgread -O JSON` of the output
  instead, because `dwg2dxf` leaves out values that equal LibreDWG's defaults.
- **ezdwg readback.** This is a separate decoder. It compares:
  - model-space entities (`entmode == 2`) with the same geometric signature;
  - dimensions, using the stored measurement;
  - texts, layers and `$INSUNITS`.
- **Fail closed.** Any difference raises `dwg_readback_mismatch`, and the
  details carry both reports.
- **Reported losses** (`DwgWriteResult.losses`):
  - the removed R2007+ objects;
  - the empty `ACAD_COLOR` dictionary;
  - aligned-TEXT inserts moved by 0.001 mm;
  - the changed model-space handle and `$HANDSEED`;
  - changed header variables (`$DIMBLK`, D11);
  - R2007+ dimension-style variables that R2000 has no field for;
  - text restructured by N0 (`dwg_cjk_text_restructured`, with the original strings).
- **Timing.** On the plate drawing in the tests, a write with both readbacks
  takes about 0.5 s. The same DXF always gives the same DWG bytes, and they
  are identical between the macOS x86_64 build and the Linux aarch64 image
  build (checked in W5b).

## Read path: DWG → derived DXF

**Before conversion.**

- The magic and version are checked.
- The file length is compared with the extent the header declares:
  - R13 to R2000 use the section-locator table;
  - R2004 and later (except R2007) use the encrypted file header, which is
    trusted only when its CRC holds, and the address of the second header.
    The page-map allocation extent is not always the physical EOF. A compact
    file ending with an exact copy of the CRC-checked primary header at the
    declared second-header address uses that physical end instead. Missing,
    truncated or mismatched copies retain the conservative extent check;
    all converter structural-error and completeness checks still run.
    Observed with an AC1032 file of 301520 bytes whose allocation extent is
    302624 and whose complete second header starts at 301412. See LibreDWG
    0.14 `src/r2004_file_header.spec` and `read_R2004_section_map` in `src/decode.c`.

This check exists because `dwg2dxf` converts truncated copies of the repo
sample with **exit code 0**: one cut to 16 KB of DXF, another cut to 25 MB
with 11k errors.

**Conversion and sanitising.** The DXF that comes back is
`role="derived"`. It is an interpretation of the DWG, not the original.

| Step | What it does | Why |
| --- | --- | --- |
| `dwg2dxf` | Converts the DWG. Fails on: exit code ≠ 0, missing output, missing EOF record, or a structural ERROR (overflow, "failed to", missing, out of bounds, CRC, decompress, section address beyond EOF). Other decoder errors become a warning; the sample has 29. | Corrupted bodies exit 0 with overflow errors. |
| S6 | Rejects output that contains C0 control bytes and re-runs the conversion, up to 3 attempts. Fails if every attempt has them. | One run on macOS appended garbage bytes, a likely decoder over-read. |
| `dwgread` JSON | Builds the decoded object model: DIMENSION/INSERT → block handle links, type counts, owners. The parser tolerates bare `nan`/`inf`. | Needed for S2 and S5. |
| S1 | For MTEXT chunk runs that `dwg2dxf` cut inside a UTF-8 sequence or a `\U+`/`\M+` escape: joins the run at byte level and splits it again on character boundaries. The bytes themselves are unchanged. | The cuts break strict decoding and lose characters such as U+8981. |
| S2 | Renames duplicated anonymous block names; the lowest handle keeps its name. Every DIMENSION/INSERT is pointed at the block record with the handle the DWG links it to. | `dwg2dxf` names several `*D` blocks the same, so dimensions show other dimensions' graphics. |
| S3 | Removes entities that have a non-finite value in a float group, together with their VERTEX/ATTRIB/SEQEND children, and reports them. Values outside drawable entities are reported only. Nothing is ever set to zero. | The sample has a NaN LINE, `D952`. |
| S1b | For a UTF-8 (R2007+) DXF only: reads bytes that are not valid UTF-8 as Latin-1. The change is reversible and is reported with samples. | LibreDWG writes U+0080–U+00FF as single bytes. ezdwg decodes `IRHD45\xb0-55\xb0` as `IRHD45°-55°`. |
| load | Loads the result with ezdxf in strict mode. `recover` is used only as a fallback and is reported as `dwg_recover_load`. | |
| S2_check | Compares every DIMENSION/INSERT block reference after loading with the DWG links. | |
| audit | Runs the ezdxf audit and records its counts. The derived DXF bytes are not rewritten. | |
| S5 | Compares decoded objects (JSON) with emitted DXF records. Owner-less objects count as stale data (S4). Entities of a class LibreDWG marks unstable are reported as unsupported, whether or not this build wrote them; this covers jogged-radius dimensions (LARGE_RADIAL_DIMENSION). Unstable object classes (DIMASSOC, ASSOC*) are a warning. Any other entity that was decoded but not emitted is a loss. | The macOS and Linux builds emit unstable classes differently. |

**Results on the repo sample** (macOS Homebrew build; AC1032, 4.9 MB):

- **Time and size:** about 4 s; 9,329 model-space entities after quarantine
  (Linux: 9,327); 211 DIMENSION.
- **Sanitising:**
  - S1 re-split 4 runs;
  - S1b reinterpreted 4 bytes;
  - S2 renamed 12 records, and all 720 block links match the DWG;
  - S3 quarantined `D952`.
- **Cross-check:** text agrees with ezdwg on 2,195 of 2,197 strings. The
  remaining 2 differ only by `^J` vs `\n`.

## Licence boundary

This is not legal advice; it needs legal review.

- **LibreDWG is GPL-3.0-or-later.** PartRoute runs only the unmodified
  command-line tools, as separate processes that exchange files. This module
  never loads `libredwg.so` into the server process: no SWIG, ctypes or cffi.
  Keep it that way.
- **If the server image or any on-prem bundle is distributed**, GPL-3
  obligations apply to the LibreDWG part:
  - the licence text, which the image installs at
    `/opt/libredwg/share/licenses/libredwg/COPYING`;
  - the corresponding source (the pinned tarball plus the build recipe in the
    Dockerfile) or a written offer. `SOURCE` in the same directory records the
    URL, the sha256 and the configure flags.
- **Pure SaaS use** of the image (Cloud Run, not distributed) carries no
  source-offer duty under GPL-3.
- **Other components:** generated DWG files are not covered by the GPL.
  ezdxf and ezdwg are MIT.

## Still open

1. **BLOCKED: independent CAD editor gate** (acceptance §5: open, edit, save
   and read back at least 3 page types). No licence-free headless DWG editor
   exists. One of these routes is needed (W5a §7):
   - teammates with licensed AutoCAD or BricsCAD;
   - Autodesk APS Design Automation;
   - ODA membership or written ODA permission.

   Until one of them runs, `verified=True` means "two independent *readers*
   agree". It does not mean "a CAD editor opened it".
2. **External-author DWGs.** Acceptance needs at least 2. The repo has 1.
3. **linux/amd64 baseline.** The read path differs between LibreDWG builds
   (S5), and the local Docker check ran on arm64 without ezdwg. Re-baseline
   the sample numbers and the write hashes in amd64 CI.
4. **AutoCAD's view of D3 and D8.** The recipe avoids both. Only a real
   AutoCAD/ODA reader can confirm the files open without recovery.
5. **Partial maps.**
   - The S5 type-name alias map (`_canon`) is partial.
   - S1b is not applied to pre-2007 (codepage) DXF. `dwg2dxf` writes ASCII
     with `\U+` escapes there.
6. **No service, API or worker wiring.** The drawing-package work owns that.
   - A DXF may be shown as a labelled interim result.
   - A DXF can never satisfy a DWG deliverable.
   - A missing runtime must be a clear task state, never a silent DXF
     fallback.
