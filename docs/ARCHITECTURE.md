# oa-git Architecture & Contract Specification (`oa-git/2`)

## 1. Design Principles

- **Official SKILL API Only**: All OpenAccess reads and writes go exclusively through documented Cadence Virtuoso SKILL APIs (`db*`, `dd*`, `sch*`, `cdf*`, `rod*`, `tech*`). Raw `.oa` binary parsing or generation is strictly forbidden.
- **Same-PDK High-Fidelity Git Representation**: Capture exact instance masters `(lib, cell, view)`, terminal bindings, typed editable CDF primary inputs, separated derived readback diagnostics, full schematic placement/wire/pin/annotation geometry, complete `schematicSymbol` contracts/graphics, and all 15 physical `maskLayout` OA object types (incl. PCells, vias, pins, shapes, blockages, boundaries, and figGroups).
- **Deep Modules & Explicit Seams**: Keep canonical serialization, semantic diff, three-way merge, integrity/lock/isomorphism guards, dirty-index tracking, and view-handler dispatch in pure, offline-testable Python. Keep Virtuoso SKILL transport isolated behind `OaBackend`, `SkillRequestRunner` (`run_skill_request`), `ViewHandler`, and `VirtuosoDaemonClient`.
- **Fail-Closed by Default**: Unsaved OA buffers (`modifiedButNotSaved`), live edit locks (`*.cdslck*` / `*.lck`), disallowed target libraries, unsupported objects, missing/read-only/contradictory/non-convergent CDF parameters, `schCheck` errors, anonymous-net topology splits/shorts, spatial layout merge overlaps, PCell/connectivity mismatches, or post-restore readback mismatches abort immediately without corrupting existing OA views.
- **Atomic Draft-and-Publish Restore**: Never open an existing OA CellView with mode `"w"`. Restore into a fresh temporary draft cell, verify `schCheck` / PCell / connectivity / CDF convergence / readback, publish via `dbCopyCellView`, close and purge the published handle, and purge/delete the draft cell.
- **Minimal Plan-A Terminal CLI**: Keep ONLY 7 OA-specific commands (`init`, `status`, `capture`, `diff`, `merge`, `restore`, `daemon`) delegating all VCS operations (`commit`, `push`, `branch`, `checkout`, `revert`) to native `git`, backed by upward `.oagit/config.json` discovery, `.oagit/index.json` dirty-file skip across all 3 views, single-session `--hier` batching, and an FD-detached zero-dependency file-spool auto-sleep Daemon (`oagit-spool/1`) with SSH `ControlMaster`.

## 2. Unified Snapshot Storage Layout (`oa-git/2`, v1 Backward-Compatible)

Every captured CellView lives in a canonical directory:

```text
<snapshots_root>/<library>/<cell>/<view>/
  manifest.json      # Unified oa-git/2 manifest across schematic, symbol, and layout
  circuit.yaml       # [schematic] Electrical Truth: ports, nets, instances (params + derived state), props, constraints
  placement.yaml     # [schematic] Placement Geometry: instance xy/orient, pins, wires, labels, annotations, sheet
  symbol.yaml        # [symbol]    Pin contract (pins + pin_figure + port_order), graphics, typed labels, selection_box, origin
  objects.jsonl      # [layout]    Deterministically sorted OA physical objects (one JSON object per line)
  definitions.jsonl  # [layout]    Optional deduplicated PCell params / via params / props definitions
  omissions.json     # [optional]  Written iff unsupported objects were encountered; deleted on complete capture
```

### Manifest v2 Schema (`manifest.json`)
- `schema`: `"oa-git/2"` (readers remain 100% backward-compatible with `"oa-git/1"`)
- `view_type`: `"schematic"` | `"symbol"` | `"layout"`
- `cellview`: `{"library": str, "cell": str, "view": str}`
- `files`: `{filename: sha256_hex}` (`circuit.yaml` + `placement.yaml` for schematic; `symbol.yaml` for symbol; `objects.jsonl` [+ `definitions.jsonl`] for layout; plus `omissions.json` iff `omissions_count > 0`)
- `content_sha256`: unified canonical SHA-256 digest computed over sorted `filename\0raw_bytes\0` across all non-manifest, non-omissions payload files (identical algorithm across `schematic`, `symbol`, and `layout` via `view_contract.compute_content_sha256`)
- `electrical_sha256`: SHA-256 of canonical `circuit.yaml` bytes (schematic only; `null` for `symbol` and `layout`)
- `placement_sha256`: SHA-256 of canonical `placement.yaml` bytes (schematic only; `null` for `symbol` and `layout`)
- `full_sha256`: deterministic aggregate SHA-256 across all view payload digests
- `omissions_count`: integer (`0` for complete capture)
- `metadata`: deterministic optional map (e.g., `cellview_type`, `port_order`, `dbu_per_uu`, `object_counts`; **no wall-clock timestamps**)

### Schematic `params` vs `derived` Separation (`circuit.yaml`)
- `params` + `param_types`: The **only** editable design-intent input parameters (dynamically evaluated editable in schematic context, differing from `baseCdf` defaults or stored on the instance).
- `derived`: Structured readback state (`status`: `"valid"` | `"stale"` | `"unavailable"`, `values`, `input_sha256`, `callback_order`, `source`).
- `diff.py` treats `derived.values` as a separate diagnostic domain (`derived_observation_changes`), excluded from `has_electrical_changes` / `--electrical-only`.
- `merge.py` merges concurrent disjoint edits to coupled primary parameters cleanly by marking `derived.status = "stale"`, which `oagit restore --recompute-derived` recomputes legitimately through official PDK callbacks.

### Anonymous Auto-Net Isomorphism
- Named nets, port nets, global nets, and bus nets are matched strictly by literal name across `restore_schematic.il`, `guards.py:verify_roundtrip`, and `diff.py`.
- Unnamed Virtuoso auto-nets (`^net[0-9]+$` not explicitly attached to a port or wire label) are matched by **1-to-1 pin-set `(inst, term)` bijection isomorphism**, eliminating false failures when `schCheck` renumbers internal nets while failing closed on any split or short (`anon_net_split` / `anon_net_short`).

### Schematic Terminal Order Roundtrip (`metadata.port_order`)
- `export_schematic.il` records the native `cv~>terminals` order in manifest `metadata.port_order`; `circuit.yaml` ports stay alphabetically canonical so electrical diffs remain order-insensitive.
- `restore_schematic.il` creates pins and ports in that recorded order, and `guards.py:verify_roundtrip` compares the declared-port subsequence, so a restore that reverses terminal order fails closed instead of passing silently.
- Snapshots without `port_order` keep the historical name-sorted restore order; an entry that is not a permutation of the snapshot terminals is rejected before any OA write.

## 3. Deep Module Boundaries & Ownership

| Module | File(s) | Responsibility | Seam / Public Interface |
|---|---|---|---|
| **Domain Contract** | `src/oa_git/model.py` | Immutable dataclasses (`Manifest`, `DerivedState`, `CdfCouplingRule`, `AnnotationShape`, `RestoreReceipt`), Fail-Closed exception hierarchy | Shared domain types (`oa-git/2`, `oa-git-config/2`) |
| **View Contract & Registry** | `src/oa_git/view_contract.py` | Unified `ViewHandler` protocol, `ensure_default_view_handlers()`, `infer_view_type()`, `OaSession`, `BatchCaptureItem/Result`, `SkillRequestRunner`, universal v2 manifest integrity verification | Auto-registers `schematic`, `symbol`, and `layout` (`LayoutHandler`) |
| **Schematic Codec / Diff / Merge / Guards** | `src/oa_git/{snapshot,diff,merge,guards}.py` | Canonical `oa-git/2` schematic codec, `compute_cdf_input_sha256`, `resolve_cdf_callback_order`, anonymous-net bijection, collinear wire coalescing, derived-aware diff/merge, live lock & roundtrip guards | `SchematicViewHandler`, `diff_schematics`, `merge_schematics`, `verify_roundtrip` |
| **Symbol Full Stack** | `src/oa_git/symbol_{model,snapshot,diff,merge}.py`, `src/oa_git/skill/{export_symbol,restore_symbol}.il` | Lightweight `symbol.yaml` (`oa-git-symbol/1`), scalar `ILList` `termOrder`, 3-domain diff (pin contract / pin geometry / graphics), 3-way merge, official SKILL symbol export & atomic restore | `SymbolViewHandler`, `diff_symbols`, `merge_symbols`, `verify_symbol_roundtrip` |
| **Physical Layout Full Stack** | `src/oa_git/layout_{model,snapshot,diff,merge,backend,cli}.py`, `src/oa_git/skill/{export_layout,restore_layout}.il` | Canonical `objects.jsonl` + `definitions.jsonl` (`oa-git-layout/1`), 15 OA physical object types, 3-domain diff (Connectivity / Geometry incl. pin geometry moves / Annotation), spatial-overlap-guarded 3-way merge, official SKILL layout export & atomic restore with PCell + connectivity + readback verification | `LayoutHandler`, `VirtuosoLayoutBackend`, `FixtureLayoutBackend`, `diff_layouts`, `merge_layouts` |
| **Project State & Dirty Index** | `src/oa_git/project_state.py` | Upward `.oagit/config.json` (fallback `.oagit.json`) discovery, `flock`-locked `.oagit/index.json` source fingerprinting across `sch.oa`, `symbol.oa`, and `layout.oa` (incl. custom `layout_*` view names) & `hierarchy_complete` tracking | `discover_project_root`, `load_project_config`, `classify_cellview_state`, `update_index_entry` |
| **Workspace & `cds.lib` Bootstrap** | `src/oa_git/workspace.py` | Pure offline top-level `cds.lib` parser (`DEFINE`/`UNDEFINE`/`ASSIGN`; intentionally skips `INCLUDE`/`SOFTINCLUDE` system files), Cadence built-in/PDK/`COMBINE`/`Invisible` exclusion, repo-local `allow_libraries` boundary, `PATH`/`$CDSHOME` Virtuoso probe, automatic `git init`, managed root-anchored `/<lib>/` and `.oagit/` runtime (`.oagit/index.json`, `.oagit/index.lock`, `.oagit/daemon/`, `.oagit/ssh/`) `.gitignore` block, and read-only `git check-ignore` health checks | Offline workspace discovery, Git/ignore bootstrap & health-check API |
| **Batch & Lazy Auto-Sleep Daemon** | `src/oa_git/{batch_backend,daemon}.py`, `src/oa_git/skill/{batch_capture,batch_restore,daemon}.il` | Single-session `oagit-batch/1` (`--hier` max depth 32 + cycle detection, inline restore+readback), `oagit-spool/1` file-spool daemon with idle auto-exit (default `3600s`, `0` disables idle auto-exit) and native `ipcSleep(1)` idle backoff, FD-detached SSH `ControlMaster` (`-n`, `setsid`, FD 3..9 close) with stale-socket/orphan hardening, single crash fallback (`one_shot_fallback`) | `VirtuosoDaemonClient`, `SshMuxManager`, `filter_dirty_targets` |
| **Virtuoso SKILL Bridge** | `src/oa_git/{bridge,virtuoso_backend}.py`, `src/oa_git/skill/{oagit_common,export_schematic,restore_schematic}.il` | Asymmetric `request.il` -> `response.json` wire protocol, `run_skill_request` seam, bounded fresh-run retry + post-exit `.cxt` teardown grace (`exit 139`), unified `capture_{schematic,symbol,layout,batch}` and `restore_{schematic,symbol,layout,batch}` | Implements `OaBackend` & `SkillRequestRunner` |
| **Minimal Unified CLI** | `src/oa_git/cli.py` *(+ backward-compat `layout_cli.py`)* | Plan-A 7-command CLI (`init`, `status`, `capture`, `restore`, `diff`, `merge`, `daemon`) transparently handling Schematic + Symbol + Layout with human output & `--json` contract | `main(argv=None) -> int` |

## 4. Minimal Unified CLI Contract (`oagit`)

Exit codes: `0` success (and for `diff --exit-code`, no changes), `1` `status` integrity/lock failures, merge conflicts, or `diff --exit-code` changes, `2` usage, safety/guard, integrity (on `restore`/`merge`/`diff`), or runtime errors. Root-level `oagit [--json] [-C PATH] <command>` and per-command flags are both supported, and `_emit_error` attaches concise actionable `Hint:` guidance (`hint` field in JSON).

- `oagit init [--v1|--v2] [--sync] [--no-git] [--no-auto-allow] [--no-auto-discover] [-C/--root PATH] [--snapshots-dir snapshots] [--cds-lib PATH] [--oa-lib LIB=DIR ...] [--allow-library LIB ...] [--backend virtuoso|fixture] [--virtuoso-cmd CMD] [--ssh-host HOST] [--remote-workdir DIR] [--timeout-s 180] [--daemon|--no-daemon] [--daemon-idle-timeout/--idle-timeout-s 3600] [--daemon-startup-timeout 30] [--daemon-poll-ms 100] [--max-passes 3] [--force] [--json]` *(defaults to v2 `.oagit/config.json`; auto-discovers `./cds.lib` libraries, repo-local `allow_libraries`, and `PATH`/`$CDSHOME` Virtuoso; runs `git init` and maintains a managed root-anchored `/<lib>/` `.gitignore` block; `--sync` refreshes mappings and `.gitignore` after creating new OA libraries)*
- `oagit status [--changed] [-C/--root PATH] [--snapshots-dir DIR] [--oa-lib/--oa-lib-map LIB=DIR ...] [--json]` *(verifies all 3 views, dynamically syncs newly added `cds.lib` libraries at read time, tracks `layout.oa` dirty state, and reports read-only `git check-ignore` `.gitignore` trap warnings)*
- `oagit capture [cellviews ...] [--view-type auto|schematic|symbol|layout] [--hier] [--include-symbols] [--changed] [--recompute-derived] [--max-depth 32] [--one-shot] [--no-daemon] [-C/--root PATH] [-o/--out DIR] [--json]` *(0 positional args defaults to incremental `--changed` across all tracked views; performs dynamic read-time `cds.lib` discovery)*
- `oagit restore <snapshot_dir_or_cellview> [--target <lib>/<cell>/<view>] [--allow-library LIB ...] [--overwrite] [--recompute-derived] [--inline-readback|--no-inline-readback] [--one-shot] [--no-daemon] [--skip-readback] [--skip-placement-check] [--allow-omissions] [-C/--root PATH] [--json]`
- `oagit diff [old_or_range] [new] [--stat] [--electrical-only] [--connectivity-only] [--contract-only] [--cellview <lib>/<cell>/<view>] [-C/--root PATH] [--exit-code] [--json]` *(0 args: `HEAD` vs working `snapshots/`; 1 arg: `REV` vs local or `OLD..NEW`; auto-discovers all changed Schematic + Symbol + Layout views)*
- `oagit merge <base> <ours> <theirs> -o <out_dir> [--allow-spatial-overlap] [-C/--root PATH] [--json]` *(accepts paths or `<lib>/<cell>/<view>` shorthand)*
- `oagit daemon [start|stop|status] [--idle-timeout-s N] [-C/--root PATH] [--json]` *(action defaults to `status`)*

## 5. Virtuoso SKILL Bridge Wire Contracts

Because Cadence SKILL has no built-in JSON parser, all protocols use an asymmetric, zero-fragility wire format:
- **Python -> SKILL**: Strictly escaped SKILL data (`request.il`) defining `oagitRequest` as a DPL (strings quoted whenever containing bus/operator characters such as `< > : + - * / ! . =`).
- **SKILL -> Python**: UTF-8 JSON (`response.json`) written via `oagit_common.il`, parsed with `json.loads()`.
- **Unified Action Dispatch (`run_skill_request`)**: Supports `capture_schematic`, `restore_schematic`, `capture_symbol`, `restore_symbol`, `capture_layout`, `restore_layout`, `capture_batch`, and `restore_batch`, transparently executing via one-shot, single-session batch, or persistent `oagit-spool/1` daemon.

Detailed sub-protocols:
- Symbol contract & `symbol.yaml`: [docs/SYMBOL.md](SYMBOL.md)
- Physical Layout contract & `objects.jsonl`: [docs/LAYOUT.md](LAYOUT.md)
- Batch (`oagit-batch/1`) & Daemon spool (`oagit-spool/1`): [docs/DAEMON.md](DAEMON.md)
