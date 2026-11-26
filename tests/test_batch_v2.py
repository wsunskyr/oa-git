"""Offline unit & contract tests for single-session batch capture/restore
and local dirty/hierarchy filtering (v0.2).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any

import pytest

from oa_git.batch_backend import (
    DEFAULT_SKIP_LIBRARIES,
    filter_dirty_targets,
    parse_batch_capture_response,
    parse_batch_restore_response,
    render_batch_capture_request_skill,
    render_batch_restore_request_skill,
    schematic_snapshot_to_restore_item,
)
from oa_git.daemon import VirtuosoDaemonClient
from oa_git.model import (
    BATCH_PROTOCOL_V1,
    SPOOL_PROTOCOL_V1,
    AnnotationShape,
    BridgeExecutionError,
    CdfCouplingRule,
    CdfParameterError,
    CellViewRef,
    DerivedState,
    Instance,
    InstancePlacement,
    Net,
    Port,
    SchemaValidationError,
    SchematicElectrical,
    SchematicPlacement,
)
from oa_git.project_state import (
    IndexEntry,
    default_source_file_candidates,
    fingerprint_source_files,
)
from oa_git.snapshot import build_schematic_snapshot, snapshot_dir_for, write_snapshot
from oa_git.virtuoso_backend import VirtuosoSkillBackend


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "src" / "oa_git" / "skill"
BATCH_CAPTURE_IL = SKILL_DIR / "batch_capture.il"
BATCH_RESTORE_IL = SKILL_DIR / "batch_restore.il"
DAEMON_IL = SKILL_DIR / "daemon.il"


def _balanced_skill_parentheses(source: str) -> None:
    depth = 0
    state = "normal"
    escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if state == "string":
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                state = "normal"
            index += 1
            continue
        if state == "line_comment":
            if char in "\r\n":
                state = "normal"
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and next_char == "/":
                state = "normal"
                index += 2
            else:
                index += 1
            continue
        if char == '"':
            state = "string"
        elif char == ";":
            state = "line_comment"
        elif char == "/" and next_char == "*":
            state = "block_comment"
            index += 1
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise AssertionError(f"unmatched closing parenthesis at offset {index}")
        index += 1
    assert state == "normal", f"unterminated SKILL {state}"
    assert depth == 0, f"unbalanced parentheses, depth={depth}"


def _make_snapshot(cv: CellViewRef):
    electrical = SchematicElectrical(
        ports=(Port(name="IN", direction="input"),),
        nets=(Net(name="IN"),),
        instances=(
            Instance(
                name="M1",
                lib="pdk",
                cell="nch",
                view="symbol",
                connections={"G": "IN"},
                params={"w": "2u"},
                param_types={"w": "string"},
            ),
        ),
    )
    placement = SchematicPlacement(
        instances=(InstancePlacement(name="M1", xy=(0.0, 0.0), orient="R0"),),
    )
    return build_schematic_snapshot(cv, electrical, placement)


def _setup_indexed_clean_cellview(
    lib_root: Path,
    snapshots_dir: Path,
    cv: CellViewRef,
    *,
    content: bytes = b"OA_DATA",
    dependencies: tuple[str, ...] = (),
    hierarchy_complete: bool = False,
) -> tuple[Path, IndexEntry]:
    oa_file = lib_root / cv.cell / cv.view / ("sch.oa" if cv.view == "schematic" else "symbol.oa")
    oa_file.parent.mkdir(parents=True, exist_ok=True)
    oa_file.write_bytes(content)

    snap = _make_snapshot(CellViewRef(cv.library, cv.cell, "schematic"))
    if cv.view == "schematic":
        write_snapshot(snap, snapshot_dir_for(snapshots_dir, cv))
        full_sha = snap.manifest.full_sha256
    else:
        # Write a valid v2 symbol manifest for symbol dirty-index checks
        from oa_git.view_contract import build_manifest_v2

        sym_dir = snapshot_dir_for(snapshots_dir, cv)
        sym_dir.mkdir(parents=True, exist_ok=True)
        sym_bytes = b"schema: oa-git-symbol/1\n"
        (sym_dir / "symbol.yaml").write_bytes(sym_bytes)
        manifest = build_manifest_v2(
            view_type="symbol",
            cellview=cv,
            file_contents={"symbol.yaml": sym_bytes},
        )
        (sym_dir / "manifest.json").write_text(
            json.dumps(manifest.to_dict(), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        full_sha = manifest.full_sha256

    cands = default_source_file_candidates(cv)
    fps = fingerprint_source_files(lib_root, cands)
    try:
        entry = IndexEntry(
            cellview=cv,
            source_files=fps,
            dependencies=dependencies,
            snapshot=f"snapshots/{cv.key}",
            snapshot_full_sha256=full_sha,
            state="clean",
            hierarchy_complete=hierarchy_complete,  # type: ignore[call-arg]
        )
    except TypeError:
        entry = IndexEntry(
            cellview=cv,
            source_files=fps,
            dependencies=dependencies,
            snapshot=f"snapshots/{cv.key}",
            snapshot_full_sha256=full_sha,
            state="clean",
        )
        object.__setattr__(entry, "hierarchy_complete", hierarchy_complete)
    return oa_file, entry


# ---------------------------------------------------------------------------
# 1. Batch Request Renderers
# ---------------------------------------------------------------------------


def test_render_batch_capture_request_skill_valid_and_invalid(tmp_path: Path) -> None:
    cvs = [
        CellViewRef("work", "top", "schematic"),
        CellViewRef("work", "top", "symbol"),
    ]
    rendered = render_batch_capture_request_skill(
        cvs,
        request_id="cap-001",
        output_path=tmp_path / "cap-001.resp.json",
        hier=True,
        max_depth=16,
        recompute_derived=True,
        include_symbols=True,
    )
    _balanced_skill_parentheses(rendered)
    assert rendered.startswith("setq(oagitRequest '")
    assert f'protocol "{BATCH_PROTOCOL_V1}"' in rendered
    assert 'action "capture_batch"' in rendered
    assert 'request_id "cap-001"' in rendered
    assert "hier t" in rendered
    assert "max_depth 16" in rendered
    assert "include_symbols t" in rendered
    for skipped in DEFAULT_SKIP_LIBRARIES:
        assert f'"{skipped}"' in rendered

    with pytest.raises(SchemaValidationError):
        render_batch_capture_request_skill([], request_id="cap-empty", output_path=tmp_path / "x.json")
    with pytest.raises(SchemaValidationError):
        render_batch_capture_request_skill(
            cvs, request_id="bad/id", output_path=tmp_path / "x.json"
        )
    with pytest.raises(SchemaValidationError):
        render_batch_capture_request_skill(
            cvs, request_id="cap-neg", output_path=tmp_path / "x.json", max_depth=-1
        )


def test_render_batch_restore_request_skill_with_snapshot_helper(tmp_path: Path) -> None:
    cv = CellViewRef("work", "opamp", "schematic")
    snap = _make_snapshot(cv)
    item = schematic_snapshot_to_restore_item(
        snap,
        cv,
        draft_cell="opamp__oagit_draft_1234",
        overwrite=True,
        allow_libraries=["work"],
        recompute_derived=True,
    )
    rendered = render_batch_restore_request_skill(
        [item],
        request_id="res-001",
        output_path=tmp_path / "res-001.resp.json",
        readback=True,
    )
    _balanced_skill_parentheses(rendered)
    assert f'protocol "{BATCH_PROTOCOL_V1}"' in rendered
    assert 'action "restore_batch"' in rendered
    assert "readback t" in rendered
    assert 'draft_cell "opamp__oagit_draft_1234"' in rendered

    with pytest.raises(SchemaValidationError):
        render_batch_restore_request_skill([], request_id="res-empty", output_path=tmp_path / "r.json")
    with pytest.raises(SchemaValidationError):
        render_batch_restore_request_skill(
            [{"target": cv, "draft_cell": "bad/draft", "allow_libraries": ["work"]}],
            request_id="res-bad",
            output_path=tmp_path / "r.json",
        )


# ---------------------------------------------------------------------------
# 2. Batch Response Parsing (Hierarchy, Cycles, Depth Errors, Partial Failure)
# ---------------------------------------------------------------------------


def test_parse_batch_capture_response_dedup_cycles_and_errors() -> None:
    payload = {
        "protocol": BATCH_PROTOCOL_V1,
        "request_id": "req-hier-1",
        "ok": True,
        "results": [
            {
                "cellview": {"library": "work", "cell": "top", "view": "schematic"},
                "ok": True,
                "capture": {"ports": [], "nets": [], "instances": []},
            },
            {
                "cellview": {"library": "work", "cell": "sub", "view": "schematic"},
                "ok": True,
                "capture": {"ports": [], "nets": [], "instances": []},
            },
        ],
        "discovered": [
            {"library": "work", "cell": "sub", "view": "schematic"},
            {"library": "work", "cell": "sub", "view": "schematic"},  # duplicate
            {"library": "work", "cell": "sub", "view": "symbol"},
        ],
        "cycles": [
            ["work/a/schematic", "work/b/schematic", "work/a/schematic"],
            ["work/a/schematic", "work/b/schematic", "work/a/schematic"],
        ],
        "errors": [],
        "session": {"daemon_id": "0123456789abcdef", "reused": True},
    }
    parsed = parse_batch_capture_response(payload, expected_request_id="req-hier-1")
    assert parsed["ok"] is True
    assert len(parsed["results"]) == 2
    assert parsed["discovered"] == [
        CellViewRef("work", "sub", "schematic"),
        CellViewRef("work", "sub", "symbol"),
    ]
    assert parsed["cycles"] == [("work/a/schematic", "work/b/schematic", "work/a/schematic")]
    assert parsed["session"]["reused"] is True


def test_parse_batch_capture_response_depth_exceeded_and_partial_failure() -> None:
    depth_payload = {
        "protocol": BATCH_PROTOCOL_V1,
        "request_id": "req-depth",
        "ok": False,
        "results": [
            {
                "cellview": {"library": "work", "cell": "top", "view": "schematic"},
                "ok": True,
                "capture": {"ports": []},
            },
            {
                "cellview": {"library": "work", "cell": "broken", "view": "schematic"},
                "ok": False,
                "error_code": "open_failed",
                "error": "unable to open",
            },
        ],
        "discovered": [],
        "cycles": [],
        "errors": [
            {
                "code": "hierarchy_depth_exceeded",
                "cellview": "work/deep33/schematic",
                "depth": 33,
                "max_depth": 32,
            }
        ],
        "session": {"daemon_id": None, "reused": False},
    }
    parsed = parse_batch_capture_response(depth_payload)
    assert parsed["ok"] is False
    assert parsed["results"][0]["ok"] is True
    assert parsed["results"][1]["ok"] is False
    assert parsed["results"][1]["error_code"] == "open_failed"
    assert parsed["errors"][0]["code"] == "hierarchy_depth_exceeded"

    # Lying response (ok=True with failed item) fails closed
    bad_claim = dict(depth_payload, ok=True)
    with pytest.raises(SchemaValidationError, match="ok=true"):
        parse_batch_capture_response(bad_claim)

    # Wrong protocol fails closed
    with pytest.raises(SchemaValidationError, match="protocol"):
        parse_batch_capture_response(dict(depth_payload, protocol="wrong/1"))


def test_parse_batch_restore_response_with_inline_readback() -> None:
    payload = {
        "protocol": BATCH_PROTOCOL_V1,
        "request_id": "res-1",
        "ok": True,
        "results": [
            {
                "target": {"library": "work", "cell": "opamp", "view": "schematic"},
                "ok": True,
                "published": True,
                "draft_cell": "opamp__oagit_draft_1",
                "sch_check": {"errors": 0, "warnings": 0},
                "cdf_verified": True,
                "connectivity_mismatches": [],
                "readback": {"ok": True, "ports": []},
            }
        ],
        "errors": [],
        "session": {"daemon_id": "abcd1234abcd1234", "reused": True},
    }
    parsed = parse_batch_restore_response(payload, expected_request_id="res-1")
    assert parsed["ok"] is True
    assert parsed["results"][0]["published"] is True
    assert parsed["results"][0]["readback"] == {"ok": True, "ports": []}

    # ok=True but published=False must fail closed
    bad = json.loads(json.dumps(payload))
    bad["results"][0]["published"] = False
    with pytest.raises(SchemaValidationError, match="published=false"):
        parse_batch_restore_response(bad)


# ---------------------------------------------------------------------------
# 3. Dirty Target & Hierarchical Clean-Subtree Filtering
# ---------------------------------------------------------------------------


def test_filter_dirty_targets_flat_and_hierarchical_subtrees(tmp_path: Path) -> None:
    lib_root = tmp_path / "oa" / "work"
    snapshots_dir = tmp_path / "snapshots"

    top = CellViewRef("work", "top", "schematic")
    sub = CellViewRef("work", "sub", "schematic")
    leaf = CellViewRef("work", "leaf", "symbol")
    dirty_cv = CellViewRef("work", "dirty_block", "schematic")

    _, entry_leaf = _setup_indexed_clean_cellview(lib_root, snapshots_dir, leaf)
    sub_oa, entry_sub = _setup_indexed_clean_cellview(
        lib_root, snapshots_dir, sub, dependencies=(leaf.key,), hierarchy_complete=True
    )
    _, entry_top = _setup_indexed_clean_cellview(
        lib_root, snapshots_dir, top, dependencies=(sub.key,), hierarchy_complete=True
    )
    dirty_oa, entry_dirty = _setup_indexed_clean_cellview(lib_root, snapshots_dir, dirty_cv)
    dirty_oa.write_bytes(b"MODIFIED_OA")

    entries = {
        top.key: entry_top,
        sub.key: entry_sub,
        leaf.key: entry_leaf,
        dirty_cv.key: entry_dirty,
    }

    # Flat filter: top clean -> skipped, dirty_cv -> dirty
    dirty_targets, skipped = filter_dirty_targets(
        [top, dirty_cv],
        oa_libs={"work": lib_root},
        snapshots_dir=snapshots_dir,
        index_entries=entries,
        hier=False,
    )
    assert dirty_targets == [dirty_cv]
    assert [item.cellview for item in skipped] == [top]
    assert skipped[0].skipped is True
    assert skipped[0].reason == "clean"

    # Hier filter: entire known subtree (top -> sub -> leaf) with hierarchy_complete=True -> skipped!
    dirty_hier, skipped_hier = filter_dirty_targets(
        [top],
        oa_libs={"work": lib_root},
        snapshots_dir=snapshots_dir,
        index_entries=entries,
        hier=True,
    )
    assert dirty_hier == []
    assert [item.cellview for item in skipped_hier] == [top]

    # mtime-only change on sub with identical SHA-256 -> subtree STILL skipped!
    st = sub_oa.stat()
    os.utime(sub_oa, ns=(st.st_atime_ns, st.st_mtime_ns + 9_000_000_000))
    dirty_mtime, skipped_mtime = filter_dirty_targets(
        [top],
        oa_libs={"work": lib_root},
        snapshots_dir=snapshots_dir,
        index_entries=entries,
        hier=True,
    )
    assert dirty_mtime == []
    assert [item.cellview for item in skipped_mtime] == [top]

    # Cycle among clean dependencies (a <-> b) must NOT loop infinitely and still skips!
    cyc_a = CellViewRef("work", "cyc_a", "schematic")
    cyc_b = CellViewRef("work", "cyc_b", "schematic")
    _, e_a = _setup_indexed_clean_cellview(
        lib_root, snapshots_dir, cyc_a, dependencies=(cyc_b.key,), hierarchy_complete=True
    )
    _, e_b = _setup_indexed_clean_cellview(
        lib_root, snapshots_dir, cyc_b, dependencies=(cyc_a.key,), hierarchy_complete=True
    )
    entries[cyc_a.key] = e_a
    entries[cyc_b.key] = e_b
    dirty_cyc, skipped_cyc = filter_dirty_targets(
        [cyc_a], oa_libs={"work": lib_root}, snapshots_dir=snapshots_dir, index_entries=entries, hier=True
    )
    assert dirty_cyc == []
    assert [item.cellview for item in skipped_cyc] == [cyc_a]

    # Modify leaf inside top's subtree -> top MUST now enter dirty_targets!
    sub_oa.write_bytes(b"SUB_CHANGED")
    dirty_after_edit, skipped_after_edit = filter_dirty_targets(
        [top], oa_libs={"work": lib_root}, snapshots_dir=snapshots_dir, index_entries=entries, hier=True
    )
    assert dirty_after_edit == [top]
    assert skipped_after_edit == []

    # Unindexed dependency -> top MUST enter dirty_targets (fail-closed)
    sub_oa.write_bytes(b"OA_DATA")
    entries_missing_dep = dict(entries)
    del entries_missing_dep[leaf.key]
    dirty_unindexed, _ = filter_dirty_targets(
        [top],
        oa_libs={"work": lib_root},
        snapshots_dir=snapshots_dir,
        index_entries=entries_missing_dep,
        hier=True,
    )
    assert dirty_unindexed == [top]


def test_filter_dirty_targets_hierarchy_complete_and_include_symbols(tmp_path: Path) -> None:
    lib_root = tmp_path / "oa" / "work"
    snapshots_dir = tmp_path / "snapshots"

    top = CellViewRef("work", "top", "schematic")
    top_sym = CellViewRef("work", "top", "symbol")
    sub = CellViewRef("work", "sub", "schematic")
    sub_sym = CellViewRef("work", "sub", "symbol")

    # Flat-indexed schematic (hierarchy_complete=False) MUST NOT be skipped under hier=True
    _, e_top_flat = _setup_indexed_clean_cellview(
        lib_root, snapshots_dir, top, dependencies=(), hierarchy_complete=False
    )
    dirty_flat_hier, skipped_flat_hier = filter_dirty_targets(
        [top],
        oa_libs={"work": lib_root},
        snapshots_dir=snapshots_dir,
        index_entries={top.key: e_top_flat},
        hier=True,
    )
    assert dirty_flat_hier == [top]
    assert skipped_flat_hier == []

    # Root complete, child schematic NOT hierarchy_complete -> MUST NOT skip
    _, e_sub_incomplete = _setup_indexed_clean_cellview(
        lib_root, snapshots_dir, sub, dependencies=(), hierarchy_complete=False
    )
    _, e_top_complete = _setup_indexed_clean_cellview(
        lib_root, snapshots_dir, top, dependencies=(sub.key,), hierarchy_complete=True
    )
    dirty_child_inc, _ = filter_dirty_targets(
        [top],
        oa_libs={"work": lib_root},
        snapshots_dir=snapshots_dir,
        index_entries={top.key: e_top_complete, sub.key: e_sub_incomplete},
        hier=True,
    )
    assert dirty_child_inc == [top]

    # include_symbols=True (flat): schematic clean but symbol absent -> MUST NOT skip!
    dirty_no_sym, _ = filter_dirty_targets(
        [top],
        oa_libs={"work": lib_root},
        snapshots_dir=snapshots_dir,
        index_entries={top.key: e_top_complete},
        hier=False,
        include_symbols=True,
    )
    assert dirty_no_sym == [top]

    # Create clean symbol for top -> flat include_symbols=True now skips top!
    top_sym_oa, e_top_sym = _setup_indexed_clean_cellview(lib_root, snapshots_dir, top_sym)
    _, e_sub_complete = _setup_indexed_clean_cellview(
        lib_root, snapshots_dir, sub, dependencies=(), hierarchy_complete=True
    )
    _, e_top_with_sym = _setup_indexed_clean_cellview(
        lib_root, snapshots_dir, top, dependencies=(sub.key,), hierarchy_complete=True
    )
    dirty_with_top_sym, skipped_with_top_sym = filter_dirty_targets(
        [top],
        oa_libs={"work": lib_root},
        snapshots_dir=snapshots_dir,
        index_entries={top.key: e_top_with_sym, top_sym.key: e_top_sym},
        hier=False,
        include_symbols=True,
    )
    assert dirty_with_top_sym == []
    assert [item.cellview for item in skipped_with_top_sym] == [top]

    # hier=True + include_symbols=True: sub's symbol still missing -> MUST NOT skip!
    entries_hier = {
        top.key: e_top_with_sym,
        top_sym.key: e_top_sym,
        sub.key: e_sub_complete,
    }
    dirty_missing_sub_sym, _ = filter_dirty_targets(
        [top],
        oa_libs={"work": lib_root},
        snapshots_dir=snapshots_dir,
        index_entries=entries_hier,
        hier=True,
        include_symbols=True,
    )
    assert dirty_missing_sub_sym == [top]

    # Add clean sub symbol -> hier=True + include_symbols=True now skips top!
    _, e_sub_sym = _setup_indexed_clean_cellview(lib_root, snapshots_dir, sub_sym)
    entries_hier[sub_sym.key] = e_sub_sym
    # Re-fingerprint top/sub schematics since symbol.oa is part of schematic source candidates
    _, e_top_refreshed = _setup_indexed_clean_cellview(
        lib_root, snapshots_dir, top, dependencies=(sub.key,), hierarchy_complete=True
    )
    _, e_sub_refreshed = _setup_indexed_clean_cellview(
        lib_root, snapshots_dir, sub, dependencies=(), hierarchy_complete=True
    )
    entries_hier[top.key] = e_top_refreshed
    entries_hier[sub.key] = e_sub_refreshed
    dirty_all_clean, skipped_all_clean = filter_dirty_targets(
        [top],
        oa_libs={"work": lib_root},
        snapshots_dir=snapshots_dir,
        index_entries=entries_hier,
        hier=True,
        include_symbols=True,
    )
    assert dirty_all_clean == []
    assert [item.cellview for item in skipped_all_clean] == [top]

    # Dirty top symbol -> MUST NOT skip!
    top_sym_oa.write_bytes(b"SYM_CHANGED")
    dirty_sym_edited, _ = filter_dirty_targets(
        [top],
        oa_libs={"work": lib_root},
        snapshots_dir=snapshots_dir,
        index_entries=entries_hier,
        hier=False,
        include_symbols=True,
    )
    assert dirty_sym_edited == [top]


def test_n_dirty_captures_single_virtuoso_launch_and_zero_when_clean(tmp_path: Path) -> None:
    """Prove N dirty captures launch Virtuoso once, and clean targets trigger 0 launches."""

    cds = tmp_path / "cds.lib"
    cds.write_text("", encoding="utf-8")
    lib_root = tmp_path / "oa" / "work"
    snapshots_dir = tmp_path / "snapshots"

    cvs = [CellViewRef("work", f"blk_{i}", "schematic") for i in range(4)]
    entries: dict[str, IndexEntry] = {}
    for cv in cvs:
        oa_file, entry = _setup_indexed_clean_cellview(lib_root, snapshots_dir, cv)
        entries[cv.key] = entry

    launches = 0

    class _Proc:
        pid = 99999

        def poll(self):
            return None

    def fake_popen(_cmd: list[str], **_kw: Any) -> _Proc:
        nonlocal launches
        launches += 1
        client.local_state_file.write_text(
            json.dumps({"protocol": SPOOL_PROTOCOL_V1, "daemon_id": client.daemon_id, "state": "ready", "pid": 99999}),
            encoding="utf-8",
        )
        return _Proc()

    def fake_sleep(_dt: float) -> None:
        for req_file in sorted(client.local_spool_dir.glob("*.req.il")):
            req_id = req_file.name[:-7]
            req_file.unlink()
            resp = {
                "protocol": BATCH_PROTOCOL_V1,
                "request_id": req_id,
                "ok": True,
                "results": [
                    {"cellview": cv.to_dict(), "ok": True, "capture": {"ports": []}}
                    for cv in cvs
                ],
                "discovered": [],
                "cycles": [],
                "errors": [],
                "session": {"daemon_id": client.daemon_id, "reused": True},
            }
            (client.local_spool_dir / f"{req_id}.resp.json").write_text(
                json.dumps(resp), encoding="utf-8"
            )

    client = VirtuosoDaemonClient(
        project_root=tmp_path,
        cds_lib=cds,
        popen_factory=fake_popen,
        sleep_fn=fake_sleep,
        pid_alive_fn=lambda _pid: True,
    )

    # All clean -> 0 dirty targets -> 0 Virtuoso launches
    dirty, skipped = filter_dirty_targets(
        cvs, oa_libs={"work": lib_root}, snapshots_dir=snapshots_dir, index_entries=entries
    )
    assert dirty == []
    assert len(skipped) == 4
    assert launches == 0

    # Mark all 4 dirty -> single batch request -> exactly 1 Virtuoso launch
    for cv in cvs:
        (lib_root / cv.cell / "schematic" / "sch.oa").write_bytes(b"UPDATED")
    dirty2, _ = filter_dirty_targets(
        cvs, oa_libs={"work": lib_root}, snapshots_dir=snapshots_dir, index_entries=entries
    )
    assert len(dirty2) == 4
    req_il = render_batch_capture_request_skill(
        dirty2,
        request_id="batch-4",
        output_path=client.local_spool_dir / "batch-4.resp.json",
    )
    raw_resp, transport = client.execute_with_fallback(
        "capture_batch",
        req_il,
        request_id="batch-4",
        one_shot_fn=lambda: (_ for _ in ()).throw(AssertionError("should not fall back")),
    )
    parsed = parse_batch_capture_response(raw_resp, expected_request_id="batch-4")
    assert transport == "daemon"
    assert launches == 1
    assert len(parsed["results"]) == 4


# ---------------------------------------------------------------------------
# 4. SKILL Static Contract Checks for batch_capture.il & batch_restore.il
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [BATCH_CAPTURE_IL, BATCH_RESTORE_IL],
    ids=["batch_capture.il", "batch_restore.il"],
)
def test_batch_skill_scripts_balance_forbidden_apis_and_prefixes(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    _balanced_skill_parentheses(source)

    for forbidden in (
        "schHi",
        "cdfGetParam",
        "cdfGetParameterList",
        "dbReplaceProp",
        "stringToChar",
        "cond((nil",
    ):
        assert forbidden not in source, f"{path.name} contains forbidden token {forbidden!r}"

    assert "cons('jsonArray" in source

    proc_names = re.findall(r"procedure\(([A-Za-z0-9_]+)\(", source)
    assert proc_names, f"{path.name} must define procedures"
    for name in proc_names:
        assert name.startswith("oagitBatch"), f"{path.name}: unexpected procedure name {name}"


def test_no_procedure_name_collisions_across_daemon_and_batch_scripts() -> None:
    seen: dict[str, str] = {}
    for path in (DAEMON_IL, BATCH_CAPTURE_IL, BATCH_RESTORE_IL):
        source = path.read_text(encoding="utf-8")
        for name in re.findall(r"procedure\(([A-Za-z0-9_]+)\(", source):
            assert name not in seen, f"duplicate SKILL procedure {name!r} in {path.name} and {seen[name]}"
            seen[name] = path.name


def test_batch_capture_and_restore_skill_semantic_contracts() -> None:
    cap_src = BATCH_CAPTURE_IL.read_text(encoding="utf-8")
    for required in (
        "oagitBatchRunCapture",
        "oagitExportBuildResponse",
        "oagitSymBuildResponse",
        "hierarchy_depth_exceeded",
        "activeStack",
        "visited",
        "cycles",
        "skipLibraries",
        "includeSymbols",
        "modifiedButNotSaved",
        "dbClose",
        "oagit-batch/1",
        "capture_batch",
    ):
        assert required in cap_src
    assert '"w")' not in cap_src  # Capture must NEVER open CellViews for write

    res_src = BATCH_RESTORE_IL.read_text(encoding="utf-8")
    for required in (
        "oagitBatchRunRestore",
        "oagitRestoreBuild",
        "oagitRestoreCleanupDraft",
        "dbCopyCellView",
        "oagitDirHasLiveLocks",
        "oagitBatchResReadbackSchematic",
        "oagitExportBuildResponse",
        "oagit-batch/1",
        "restore_batch",
        "draftCell",
        '"readback"',
    ):
        assert required in res_src
    # Restore must NEVER open target cell in write mode (only draftCell)
    assert 'dbOpenCellViewByType(lib cell view "schematic" "w")' not in res_src
    assert 'dbOpenCellViewByType(lib draftCell view "schematic" "w")' in res_src
    # Fix #4: published CellView handle must be closed & purged
    assert "pubCv = car(copyResult)" in res_src
    assert "oagitBatchResClosePublished(pubCv lib cell view)" in res_src
    assert "errset(dbClose(pubCv) t)" in res_src
    assert "errset(dbPurge(pubCv) t)" in res_src
    # Fix #5: CDF and symbol telemetry emitted in batch_restore.il
    for telemetry_key in (
        '"cdf_passes"',
        '"callback_order"',
        '"derived_status"',
        '"derived_refresh"',
        '"pin_contract_verified"',
        '"pin_mismatches"',
        '"symbol_check"',
    ):
        assert telemetry_key in res_src, f"batch_restore.il missing {telemetry_key}"
    # Fix #9: explicit empty skip_libraries=nil checked via member('skip_libraries opts)
    assert "member('skip_libraries opts)" in cap_src


def test_schematic_snapshot_to_restore_item_populates_annotations_param_order_and_max_passes() -> None:
    cv = CellViewRef("work", "nmos_blk", "schematic")
    target = CellViewRef("sandbox", "nmos_blk", "schematic")
    rect_ann = AnnotationShape(
        kind="rect",
        layer="text",
        purpose="drawing",
        points=((0.0, 0.0), (2.0, 1.0)),
        width=0.0,
    )
    snap = build_schematic_snapshot(
        cv,
        SchematicElectrical(
            ports=(Port("IN", "input"),),
            nets=(Net("IN"),),
            instances=(
                Instance(
                    name="M1",
                    lib="pdk",
                    cell="nch",
                    view="symbol",
                    connections={"G": "IN"},
                    params={"w": "2u", "fingers": 4},
                    param_types={"w": "string", "fingers": "int"},
                    derived=DerivedState(status="valid", callback_order=("w", "fingers")),
                ),
            ),
        ),
        SchematicPlacement(
            instances=(InstancePlacement("M1", (0.0, 0.0), "R0"),),
            annotations=(rect_ann,),
        ),
    )
    rule = CdfCouplingRule(
        name="mos_order",
        members=("w", "fingers"),
        primary=("w", "fingers"),
        order=("fingers", "w"),
        dependencies={"w": ("fingers",), "fingers": ()},
    )
    item = schematic_snapshot_to_restore_item(
        snap,
        target,
        draft_cell="nmos_blk__draft",
        overwrite=True,
        allow_libraries=("sandbox",),
        recompute_derived=True,
        max_passes=5,
        coupling_rules={"pdk/nch": (rule,)},
    )
    assert item["max_passes"] == 5
    assert item["instances"][0]["param_order"] == ["fingers", "w"]
    assert item["annotations"] == [
        {
            "kind": "rect",
            "layer": "text",
            "purpose": "drawing",
            "points": [[0.0, 0.0], [2.0, 1.0]],
            "width": 0.0,
        }
    ]

    cyclic_rule = CdfCouplingRule(
        name="bad_cycle",
        members=("w", "fingers"),
        primary=("w", "fingers"),
        dependencies={"w": ("fingers",), "fingers": ("w",)},
    )
    with pytest.raises(CdfParameterError, match="cdf_order_cycle"):
        schematic_snapshot_to_restore_item(
            snap,
            target,
            draft_cell="nmos_blk__draft",
            coupling_rules={"pdk/nch": (cyclic_rule,)},
        )


def test_batch_error_payload_without_results_preserves_error_code_and_message(
    tmp_path: Path,
) -> None:
    import subprocess

    # 1) parse_batch_capture_response with ok=False and NO 'results'
    cap_err = {
        "protocol": BATCH_PROTOCOL_V1,
        "request_id": "cap-err-1",
        "ok": False,
        "error_code": "action_unavailable",
        "error": "batch_capture.il is not loaded",
        "session": {"daemon_id": "deadbeef01234567", "reused": True},
    }
    parsed_cap = parse_batch_capture_response(cap_err, expected_request_id="cap-err-1")
    assert parsed_cap["ok"] is False
    assert parsed_cap["error_code"] == "action_unavailable"
    assert parsed_cap["error"] == "batch_capture.il is not loaded"
    assert parsed_cap["results"] == []
    assert parsed_cap["errors"][0]["code"] == "action_unavailable"

    # Driver fallback without protocol/request_id/results
    driver_fallback = {
        "ok": False,
        "error_code": "driver_error",
        "error": "SKILL action load failed",
    }
    parsed_fb = parse_batch_capture_response(driver_fallback, expected_request_id="cap-fb")
    assert parsed_fb["ok"] is False
    assert parsed_fb["error_code"] == "driver_error"
    assert parsed_fb["error"] == "SKILL action load failed"

    # 2) parse_batch_restore_response with ok=False and NO 'results'
    res_err = {
        "protocol": BATCH_PROTOCOL_V1,
        "request_id": "res-err-1",
        "ok": False,
        "error_code": "batch_restore_failed",
        "error": "restore exploded",
        "session": {"daemon_id": "deadbeef01234567", "reused": True},
    }
    parsed_res = parse_batch_restore_response(res_err, expected_request_id="res-err-1")
    assert parsed_res["ok"] is False
    assert parsed_res["error_code"] == "batch_restore_failed"
    assert parsed_res["error"] == "restore exploded"
    assert parsed_res["results"] == []

    # 3) VirtuosoSkillBackend.capture_batch surfaces real error_code/error (not empty ok=True!)
    cds = tmp_path / "cds.lib"
    cds.write_text("", encoding="utf-8")

    def fail_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_dir = Path(str(kwargs["cwd"]))
        req_text = (run_dir / "request.il").read_text(encoding="utf-8")
        req_id = req_text.split('request_id "', 1)[1].split('"', 1)[0]
        (run_dir / "response.json").write_text(
            json.dumps(
                {
                    "protocol": BATCH_PROTOCOL_V1,
                    "request_id": req_id,
                    "ok": False,
                    "error_code": "action_unavailable",
                    "error": "batch_capture.il is not loaded",
                    "session": {"daemon_id": None, "reused": False},
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    backend = VirtuosoSkillBackend(runner=fail_runner)
    with pytest.raises(BridgeExecutionError, match="action_unavailable.*batch_capture\\.il"):
        backend.capture_batch([CellViewRef("work", "top", "schematic")], cds_lib=cds)


def test_parse_batch_restore_preserves_cdf_and_symbol_telemetry_and_virtuoso_propagates(
    tmp_path: Path,
) -> None:
    import subprocess

    # 1) parse_batch_restore_response preserves schematic & symbol telemetry
    payload = {
        "protocol": BATCH_PROTOCOL_V1,
        "request_id": "res-tel-1",
        "ok": True,
        "results": [
            {
                "target": {"library": "sandbox", "cell": "opamp", "view": "schematic"},
                "ok": True,
                "published": True,
                "draft_cell": "opamp__draft_1",
                "sch_check": {"errors": 0, "warnings": 0},
                "cdf_verified": True,
                "cdf_passes": 2,
                "callback_order": {"M1": ["w", "fingers"]},
                "derived_status": "valid",
                "derived_refresh": {"M1": {"wf": "1u"}},
                "connectivity_mismatches": [],
                "readback": {"ok": True},
            },
            {
                "target": {"library": "sandbox", "cell": "opamp", "view": "symbol"},
                "ok": True,
                "published": True,
                "draft_cell": "opamp__draft_2",
                "sch_check": {"errors": 0, "warnings": 0},
                "cdf_verified": False,
                "pin_contract_verified": True,
                "pin_mismatches": [],
                "symbol_check": {"errors": 0, "warnings": 1},
                "connectivity_mismatches": [],
                "readback": {"ok": True},
            },
        ],
        "errors": [],
        "session": {"daemon_id": "abcd1234abcd1234", "reused": True},
    }
    parsed = parse_batch_restore_response(payload, expected_request_id="res-tel-1")
    sch_item = parsed["results"][0]
    assert sch_item["cdf_passes"] == 2
    assert sch_item["callback_order"] == {"M1": ["w", "fingers"]}
    assert sch_item["derived_status"] == "valid"
    assert sch_item["derived_refresh"] == {"M1": {"wf": "1u"}}

    sym_item = parsed["results"][1]
    assert sym_item["pin_contract_verified"] is True
    assert sym_item["pin_mismatches"] == []
    assert sym_item["symbol_check"] == {"errors": 0, "warnings": 1}

    # 2) VirtuosoSkillBackend propagates telemetry in inline_readback AND daemon paths
    cv = CellViewRef("work", "opamp", "schematic")
    target = CellViewRef("sandbox", "opamp", "schematic")
    snap = _make_snapshot(cv)
    cds = tmp_path / "cds.lib"
    cds.write_text("", encoding="utf-8")
    raw_rb = {
        "ok": True,
        "error": None,
        "cellview": target.to_dict(),
        "modified_but_not_saved": False,
        "metadata": {"cellview_type": "schematic", "dbu_per_uu": 160.0},
        "ports": [{"name": "IN", "direction": "input"}],
        "nets": [{"name": "IN", "is_global": False, "num_bits": 1}],
        "instances": [
            {
                "name": "M1",
                "lib": "pdk",
                "cell": "nch",
                "view": "symbol",
                "xy": [0.0, 0.0],
                "orient": "R0",
                "connections": {"G": "IN"},
                "params": {"w": "2u"},
                "param_types": {"w": "string"},
                "derived_params": {"wf": "1u"},
                "props": {},
            }
        ],
        "pins": [],
        "wires": [],
        "labels": [],
        "properties": {},
        "constraints": {},
        "sheet": {},
        "omissions": [],
    }

    def inline_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_dir = Path(str(kwargs["cwd"]))
        req_text = (run_dir / "request.il").read_text(encoding="utf-8")
        req_id = req_text.split('request_id "', 1)[1].split('"', 1)[0]
        resp = {
            "protocol": BATCH_PROTOCOL_V1,
            "request_id": req_id,
            "ok": True,
            "results": [
                {
                    "target": target.to_dict(),
                    "ok": True,
                    "published": True,
                    "draft_cell": "opamp__draft_inline",
                    "sch_check": {"errors": 0, "warnings": 0},
                    "cdf_verified": True,
                    "cdf_passes": 3,
                    "callback_order": {"M1": ["w"]},
                    "derived_status": "valid",
                    "derived_refresh": {"M1": {"wf": "1u"}},
                    "connectivity_mismatches": [],
                    "readback": raw_rb,
                }
            ],
            "errors": [],
            "session": {"daemon_id": None, "reused": False},
        }
        (run_dir / "response.json").write_text(json.dumps(resp), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    inline_backend = VirtuosoSkillBackend(runner=inline_runner, inline_readback=True)
    receipt_inline = inline_backend.restore_schematic(
        snap,
        target,
        cds_lib=cds,
        allow_libraries=("sandbox",),
        recompute_derived=True,
        verify_readback=True,
    )
    assert receipt_inline.cdf_passes == 3
    assert receipt_inline.callback_order == {"M1": ["w"]}
    assert receipt_inline.derived_status == "valid"
    assert receipt_inline.derived_refresh == {"M1": {"wf": "1u"}}

    # Daemon path propagation
    class _LiveProc:
        pid = 51515

        def poll(self):
            return None

    daemon_backend = VirtuosoSkillBackend(
        daemon_enabled=True,
        project_root=tmp_path,
        popen_factory=lambda *a, **k: _LiveProc(),
    )
    client = daemon_backend.get_daemon_client(cds)
    client._prepare_local_dirs()
    client._proc = _LiveProc()
    client._write_local_state("ready", pid=51515)
    client.pid_alive_fn = lambda _p: True

    def fake_daemon_sleep(_dt: float) -> None:
        for req_file in sorted(client.local_spool_dir.glob("*.req.il")):
            req_id = req_file.name[:-7]
            req_file.unlink()
            resp = {
                "protocol": SPOOL_PROTOCOL_V1,
                "request_id": req_id,
                "ok": True,
                "action": "restore_schematic",
                "result": {
                    "target": target.to_dict(),
                    "ok": True,
                    "published": True,
                    "draft_cell": "opamp__draft_daemon",
                    "sch_check": {"errors": 0, "warnings": 0},
                    "cdf_verified": True,
                    "cdf_passes": 2,
                    "callback_order": {"M1": ["w"]},
                    "derived_status": "valid",
                    "derived_refresh": {"M1": {"wf": "1u"}},
                    "connectivity_mismatches": [],
                    "readback": raw_rb,
                },
                "session": {"daemon_id": client.daemon_id, "reused": True},
            }
            (client.local_spool_dir / f"{req_id}.resp.json").write_text(
                json.dumps(resp), encoding="utf-8"
            )

    client.sleep_fn = fake_daemon_sleep
    receipt_daemon = daemon_backend.restore_schematic(
        snap,
        target,
        cds_lib=cds,
        allow_libraries=("sandbox",),
        recompute_derived=True,
        verify_readback=True,
    )
    assert receipt_daemon.transport == "daemon"
    assert receipt_daemon.cdf_passes == 2
    assert receipt_daemon.callback_order == {"M1": ["w"]}
    assert receipt_daemon.derived_status == "valid"
    assert receipt_daemon.derived_refresh == {"M1": {"wf": "1u"}}


def test_layout_batch_telemetry_skill_parity_and_unified_backend_methods(
    tmp_path: Path,
) -> None:
    import subprocess
    from oa_git.bridge import FixtureBackend
    from oa_git.layout_backend import raw_layout_capture_to_snapshot
    from oa_git.layout_model import LayoutSnapshot

    # 1) SKILL parity assertions in batch_capture.il, batch_restore.il, export_layout.il, restore_layout.il
    cap_src = BATCH_CAPTURE_IL.read_text(encoding="utf-8")
    assert "oagitLayoutExportBuildResponse" in cap_src
    assert "oagitLayoutBuildResponse" not in cap_src
    assert 'rexMatchp("^layout" view)' in cap_src
    assert 'equal(cv~>cellViewType "maskLayout")' in cap_src

    res_src = BATCH_RESTORE_IL.read_text(encoding="utf-8")
    assert 'equal(viewType "layout")' in res_src
    assert 'rexMatchp("^layout" view)' in res_src
    assert "oagitLayoutRestoreItem" in res_src

    exp_lay = (SKILL_DIR / "export_layout.il").read_text(encoding="utf-8")
    res_lay = (SKILL_DIR / "restore_layout.il").read_text(encoding="utf-8")
    assert "stringp(oagitRequest->output_path) && isFile(oagitRequest->output_path)" in exp_lay
    assert "stringp(oagitRequest->output_path) && isFile(oagitRequest->output_path)" in res_lay
    assert "procedure(oagitLayoutRestoreItem(" in res_lay
    assert "pubCv = car(copyResult)" in res_lay
    assert "errset(dbClose(pubCv) t)" in res_lay
    assert "errset(dbPurge(pubCv) t)" in res_lay

    # 2) parse_batch_restore_response preserves layout telemetry fields
    layout_batch_payload = {
        "protocol": BATCH_PROTOCOL_V1,
        "request_id": "res-lay-1",
        "ok": True,
        "results": [
            {
                "target": {"library": "sandbox", "cell": "inv_dut", "view": "layout_pulsic"},
                "ok": True,
                "published": True,
                "draft_cell": "inv_dut__draft_1",
                "pcell_verified": True,
                "connectivity_verified": True,
                "object_counts": {"instances": 2, "nets": 4, "vias": 2},
                "pcell_mismatches": [],
                "connectivity_mismatches": [],
                "readback": {"ok": True, "objects": []},
            }
        ],
        "errors": [],
        "session": {"daemon_id": None, "reused": False},
    }
    parsed = parse_batch_restore_response(layout_batch_payload, expected_request_id="res-lay-1")
    lay_item = parsed["results"][0]
    assert lay_item["pcell_verified"] is True
    assert lay_item["connectivity_verified"] is True
    assert lay_item["object_counts"] == {"instances": 2, "nets": 4, "vias": 2}
    assert lay_item["pcell_mismatches"] == []
    assert lay_item["connectivity_mismatches"] == []

    # 3) VirtuosoSkillBackend: str local cds_lib, post-exit .cxt teardown grace,
    #    capture_layout/restore_layout delegation, and layout inside capture_batch
    lay_cv = CellViewRef("work", "inv_dut", "layout_pulsic")
    lay_target = CellViewRef("sandbox", "inv_dut", "layout_pulsic")
    raw_lay = {
        "ok": True,
        "error": None,
        "error_code": None,
        "cellview": lay_cv.to_dict(),
        "modified_but_not_saved": False,
        "metadata": {"cellview_type": "maskLayout", "dbu_per_uu": 1000.0},
        "objects": [
            {"type": "rect", "layer": "M1", "purpose": "drawing", "bbox": [[0.0, 0.0], [1.0, 1.0]]}
        ],
        "properties": {},
        "omissions": [],
    }
    cds_str = str(tmp_path / "cds.lib")
    (tmp_path / "cds.lib").write_text("", encoding="utf-8")

    seen_drivers: list[str] = []

    def layout_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_dir = Path(str(kwargs["cwd"]))
        req_text = (run_dir / "request.il").read_text(encoding="utf-8")
        driver_text = (run_dir / "driver.il").read_text(encoding="utf-8")
        seen_drivers.append(driver_text)
        if 'action "capture_batch"' in req_text:
            req_id = req_text.split('request_id "', 1)[1].split('"', 1)[0]
            resp = {
                "protocol": BATCH_PROTOCOL_V1,
                "request_id": req_id,
                "ok": True,
                "results": [
                    {"cellview": lay_cv.to_dict(), "ok": True, "capture": raw_lay}
                ],
                "discovered": [],
                "cycles": [],
                "errors": [],
                "session": {"daemon_id": None, "reused": False},
            }
            (run_dir / "response.json").write_text(json.dumps(resp), encoding="utf-8")
            # Simulate IC26.1 post-exit .cxt teardown SIGSEGV (exit 139 after Memory report: on exit)
            (run_dir / "virtuoso.log").write_text(
                "Memory report: on exit\n", encoding="utf-8"
            )
            return subprocess.CompletedProcess(cmd, 139, "", "corrupted double-linked list\n")
        if 'action "restore_layout"' in req_text:
            resp = {
                "ok": True,
                "error": None,
                "error_code": None,
                "target": lay_target.to_dict(),
                "published": True,
                "draft_cell": "inv_dut__draft_1",
                "pcell_verified": True,
                "connectivity_verified": True,
                "object_counts": {"shapes": 1},
                "pcell_mismatches": [],
                "connectivity_mismatches": [],
            }
            (run_dir / "response.json").write_text(json.dumps(resp), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        # capture_layout
        is_target = '"sandbox"' in req_text
        payload_out = dict(raw_lay, cellview=(lay_target if is_target else lay_cv).to_dict())
        (run_dir / "response.json").write_text(json.dumps(payload_out), encoding="utf-8")
        (run_dir / "virtuoso.log").write_text("Memory report: on exit\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 139, "", "Segmentation fault\n")

    vb = VirtuosoSkillBackend(runner=layout_runner)
    # Local str cds_lib accepted; empty str rejected
    with pytest.raises(BridgeExecutionError):
        vb.capture_layout(lay_cv, cds_lib="   ")

    batch_res = vb.capture_batch([lay_cv], cds_lib=cds_str)
    assert batch_res.ok is True
    assert isinstance(batch_res.items[0].snapshot, LayoutSnapshot)
    assert "export_layout.il" in seen_drivers[0]
    assert vb.last_teardown_warning is not None
    assert vb.last_teardown_warning["returncode"] == 139

    cap_snap = vb.capture_layout(lay_cv, cds_lib=cds_str)
    assert isinstance(cap_snap, LayoutSnapshot)
    receipt = vb.restore_layout(
        cap_snap,
        target=lay_target,
        cds_lib=cds_str,
        allow_libraries=("sandbox",),
    )
    assert receipt.published is True
    assert receipt.readback_verified is True

    # 4) FixtureBackend supports capture_layout, restore_layout, and layout in capture_batch
    lay_snap = raw_layout_capture_to_snapshot(raw_lay)
    fb = FixtureBackend({lay_cv: lay_snap})
    assert fb.capture_layout(lay_cv) == lay_snap
    fb_batch = fb.capture_batch([lay_cv])
    assert fb_batch.ok is True
    assert fb_batch.items[0].snapshot == lay_snap
    fb_receipt = fb.restore_layout(
        lay_snap, target=lay_target, allow_libraries=("sandbox",)
    )
    assert fb_receipt.published is True
    assert fb_receipt.readback_verified is True
    assert fb.capture_layout(lay_target).manifest.cellview == lay_target


def test_schematic_snapshot_to_restore_item_carries_captured_port_order() -> None:
    cv = CellViewRef("work", "buf", "schematic")
    target = CellViewRef("sandbox", "buf", "schematic")
    snap = build_schematic_snapshot(
        cv,
        SchematicElectrical(
            ports=(Port("IN", "input"), Port("OUT", "output")),
            nets=(Net("IN"), Net("OUT")),
        ),
        SchematicPlacement(instances=()),
        metadata={"port_order": ["OUT", "IN"]},
    )

    item = schematic_snapshot_to_restore_item(
        snap,
        target,
        draft_cell="buf__oagit_draft_001",
    )

    assert item["port_order"] == ["OUT", "IN"]
