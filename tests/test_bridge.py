from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from oa_git.bridge import (
    FixtureBackend,
    load_backend,
    parse_restore_response,
    raw_capture_to_snapshot,
    render_capture_request_skill,
    render_restore_request_skill,
    to_skill_literal,
)
from oa_git.guards import verify_roundtrip
from oa_git.model import (
    BridgeExecutionError,
    CdfParameterError,
    CellViewRef,
    DisallowedLibraryError,
    FailClosedError,
    Instance,
    InstancePlacement,
    LiveLockError,
    Net,
    NoteLabel,
    OmissionRecord,
    PinPlacement,
    Port,
    RoundtripMismatchError,
    SchemaValidationError,
    SchematicElectrical,
    SchematicPlacement,
    UnsupportedObjectError,
    WireLabel,
    WireSegment,
)
from oa_git.snapshot import build_schematic_snapshot, write_snapshot


CELLVIEW = CellViewRef("work", "amp", "schematic")
TARGET = CellViewRef("sandbox", "amp", "schematic")


def make_snapshot(cellview: CellViewRef = CELLVIEW, *, omissions=()):
    return build_schematic_snapshot(
        cellview,
        SchematicElectrical(
            ports=(Port("in", "input"), Port("out", "output")),
            nets=(Net("in"), Net("out")),
            instances=(
                Instance(
                    "R1",
                    "basic",
                    "res",
                    params={"r": "2u", "enabled": True, "count": 2, "ratio": 1.5},
                    param_types={
                        "r": "string",
                        "enabled": "boolean",
                        "count": "int",
                        "ratio": "float",
                    },
                    connections={"p": "in", "n": "out"},
                    props={"sim": "yes"},
                ),
            ),
            properties={"annotate": True},
            constraints={"check": {"enabled": True}},
        ),
        SchematicPlacement(
            instances=(InstancePlacement("R1", (1.25, 2.5), "R90"),),
            pins=(
                PinPlacement(
                    "in",
                    "in",
                    "input",
                    "basic",
                    "ipin",
                    "symbol",
                    (0.0, 2.5),
                ),
            ),
            wires=(
                WireSegment(
                    ((0.0, 2.5), (1.25, 2.5)),
                    net="in",
                    width=0.1,
                    route_method="full",
                    labels=(WireLabel("IN", (0.5, 2.5)),),
                ),
            ),
            labels=(NoteLabel("note", (3.0, 4.0)),),
            sheet={"width": 11, "height": 8.5},
        ),
        omissions=tuple(omissions),
        metadata={"cellview_type": "schematic", "dbu_per_uu": 160.0},
    )


def raw_capture_payload(cellview: CellViewRef = CELLVIEW) -> dict[str, object]:
    return {
        "ok": True,
        "error": None,
        "cellview": cellview.to_dict(),
        "modified_but_not_saved": False,
        "metadata": {"cellview_type": "schematic", "dbu_per_uu": 160.0},
        "ports": [{"name": "in", "direction": "input"}],
        "nets": [{"name": "in", "is_global": False, "num_bits": 1}],
        "instances": [
            {
                "name": "M1",
                "lib": "pdklib",
                "cell": "nmos",
                "view": "symbol",
                "xy": [1.0, 2.0],
                "orient": "R0",
                "connections": {"D": "out", "G": "in"},
                "params": {"w": "2u", "fingers": 2},
                "param_types": {"w": "string", "fingers": "int"},
                "derived_params": {"wf": "2u"},
                "props": {"simM": "1"},
            }
        ],
        "pins": [
            {
                "name": "in",
                "net": "in",
                "direction": "input",
                "lib": "basic",
                "cell": "ipin",
                "view": "symbol",
                "xy": [0.0, 2.0],
                "orient": "R0",
            }
        ],
        "wires": [
            {
                "points": [[0.0, 2.0], [1.0, 2.0]],
                "net": "in",
                "width": 0.0,
                "route_method": "full",
                "labels": [
                    {
                        "text": "IN",
                        "xy": [0.5, 2.0],
                        "justify": "centerCenter",
                        "orient": "R0",
                        "font": "stick",
                        "height": 0.0625,
                    }
                ],
            }
        ],
        "labels": [
            {
                "text": "Note",
                "xy": [0.0, 4.0],
                "layer": "text",
                "purpose": "drawing",
                "justify": "lowerLeft",
                "orient": "R0",
                "font": "stick",
                "height": 0.0625,
            }
        ],
        "properties": {"annotate": True},
        "constraints": {"check": {"enabled": True}},
        "sheet": {"width": 11, "height": 8.5},
        "omissions": [],
    }


def test_to_skill_literal_escapes_scalars_collections_and_dpls() -> None:
    value = {
        "quote": 'a"b',
        "slash": r"a\b",
        "lines": "a\nb\r\tc",
        "enabled": True,
        "disabled": False,
        "missing": None,
        "number": 1.25,
        "items": ["x", 2],
    }
    rendered = to_skill_literal(value)

    assert rendered.startswith("(nil ")
    assert 'quote "a\\"b"' in rendered
    assert 'slash "a\\\\b"' in rendered
    assert 'lines "a\\nb\\r\\tc"' in rendered
    assert "enabled t" in rendered
    assert "disabled nil" in rendered
    assert "missing nil" in rendered
    assert "number 1.25" in rendered
    assert "items (\"x\" 2)" in rendered
    assert to_skill_literal([]) == "nil"
    assert to_skill_literal(()) == "nil"
    assert to_skill_literal((1, False)) == "(1 nil)"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_to_skill_literal_rejects_nonfinite_numbers(value: float) -> None:
    with pytest.raises(SchemaValidationError):
        to_skill_literal(value)


@pytest.mark.parametrize("value", [object(), {1: "bad"}, {"x": object()}])
def test_to_skill_literal_rejects_unsupported_values(value: object) -> None:
    with pytest.raises(SchemaValidationError):
        to_skill_literal(value)


def test_capture_request_rendering_uses_absolute_path_and_dpl() -> None:
    rendered = render_capture_request_skill(CELLVIEW, Path("response.json"))

    assert rendered.startswith("setq(oagitRequest")
    assert "action \"capture_schematic\"" in rendered
    assert "library \"work\"" in rendered
    assert "cell \"amp\"" in rendered
    assert "view \"schematic\"" in rendered
    assert 'output_path "' in rendered
    assert str(Path("response.json").resolve()) in rendered


def test_restore_request_preserves_typed_params_and_all_geometry() -> None:
    rendered = render_restore_request_skill(
        make_snapshot(),
        TARGET,
        draft_cell="amp__oagit_draft_001",
        overwrite=True,
        allow_libraries=("sandbox",),
        output_path=Path("restore.json"),
    )

    assert "action \"restore_schematic\"" in rendered
    assert "target (nil library \"sandbox\" cell \"amp\" view \"schematic\")" in rendered
    assert "draft_cell \"amp__oagit_draft_001\"" in rendered
    assert "overwrite t" in rendered
    assert "allow_libraries (\"sandbox\")" in rendered
    assert "params ((count 2 int) (enabled t boolean) (r \"2u\" string) (ratio 1.5 float))" in rendered
    assert "connections ((n \"out\") (p \"in\"))" in rendered
    assert "pins ((nil name \"in\"" in rendered
    assert "wires ((nil points ((0.0 2.5) (1.25 2.5))" in rendered
    assert "labels ((nil text \"note\"" in rendered
    assert "properties ((annotate t boolean))" in rendered


def test_restore_request_encodes_property_types_including_false() -> None:
    source = make_snapshot()
    electrical = replace(
        source.electrical,
        instances=(
            replace(
                source.electrical.instances[0],
                props={
                    "enabled": False,
                    "count": 2,
                    "ratio": 1.5,
                    "label": "M1",
                },
            ),
        ),
        properties={"enabled": False, "count": 2, "ratio": 1.5, "label": "top"},
    )
    snapshot = build_schematic_snapshot(
        source.manifest.cellview,
        electrical,
        source.placement,
        metadata=source.manifest.metadata,
    )

    rendered = render_restore_request_skill(
        snapshot,
        TARGET,
        draft_cell="amp__oagit_draft_001",
        overwrite=True,
        allow_libraries=("sandbox",),
        output_path=Path("restore.json"),
    )

    assert (
        "props ((count 2 int) (enabled nil boolean) (label \"M1\" string) "
        "(ratio 1.5 float))"
        in rendered
    )
    assert (
        "properties ((count 2 int) (enabled nil boolean) (label \"top\" string) "
        "(ratio 1.5 float))"
        in rendered
    )


def test_raw_capture_to_snapshot_converts_pin_wire_and_cdf_data() -> None:
    snapshot = raw_capture_to_snapshot(raw_capture_payload())

    assert snapshot.manifest.cellview == CELLVIEW
    assert snapshot.electrical.instances[0].params == {"fingers": 2, "w": "2u"}
    assert snapshot.electrical.instances[0].param_types == {"fingers": "int", "w": "string"}
    assert snapshot.electrical.instances[0].derived_params == {"wf": "2u"}
    assert snapshot.placement.pins[0].cell == "ipin"
    assert snapshot.placement.wires[0].labels[0].text == "IN"
    assert snapshot.placement.labels[0].text == "Note"
    assert snapshot.manifest.metadata["dbu_per_uu"] == 160.0


def test_raw_capture_is_fail_closed_for_unsaved_buffers_and_backend_errors() -> None:
    modified = raw_capture_payload()
    modified["modified_but_not_saved"] = True
    with pytest.raises(FailClosedError):
        raw_capture_to_snapshot(modified)

    failed = raw_capture_payload()
    failed["ok"] = False
    failed["error"] = "SKILL export failed"
    with pytest.raises(BridgeExecutionError, match="SKILL export failed"):
        raw_capture_to_snapshot(failed)


@pytest.mark.parametrize(
    ("error_code", "error_type"),
    [
        ("disallowed_library", DisallowedLibraryError),
        ("live_lock", LiveLockError),
        ("cdf_parameter_error", CdfParameterError),
        ("unsupported_object", UnsupportedObjectError),
        ("connectivity_mismatch", RoundtripMismatchError),
        ("sch_check_failed", RoundtripMismatchError),
        ("other", BridgeExecutionError),
    ],
)
def test_parse_restore_response_maps_error_codes(error_code: str, error_type: type[Exception]) -> None:
    with pytest.raises(error_type, match=error_code):
        parse_restore_response(
            {"ok": False, "error_code": error_code, "error": "blocked"},
            TARGET,
        )


def test_parse_restore_response_returns_receipt_and_rejects_check_failures() -> None:
    receipt = parse_restore_response(
        {
            "ok": True,
            "target": TARGET.to_dict(),
            "published": True,
            "draft_cell": "amp__oagit_draft_001",
            "sch_check": {"errors": 0, "warnings": 2},
            "cdf_verified": True,
            "readback_verified": True,
            "connectivity_mismatches": [],
        },
        TARGET,
    )
    assert receipt.target == TARGET
    assert receipt.published
    assert receipt.sch_check_warnings == 2
    assert receipt.cdf_verified and receipt.readback_verified

    with pytest.raises(RoundtripMismatchError):
        parse_restore_response(
            {
                "ok": True,
                "target": TARGET.to_dict(),
                "published": True,
                "sch_check": {"errors": 1, "warnings": 0},
            },
            TARGET,
        )


def test_fixture_backend_enforces_full_guard_chain_and_rolls_back_on_drift(tmp_path: Path) -> None:
    source = make_snapshot()
    backend = FixtureBackend({CELLVIEW: source})

    with pytest.raises(DisallowedLibraryError):
        backend.restore_schematic(source, TARGET)
    with pytest.raises(BridgeExecutionError, match="no capture"):
        backend.capture_schematic(TARGET)

    lock_dir = tmp_path / "oa-view"
    lock_dir.mkdir()
    (lock_dir / "cell.cdslck").write_text("lock", encoding="utf-8")
    with pytest.raises(LiveLockError):
        backend.restore_schematic(
            source,
            TARGET,
            allow_libraries=("sandbox",),
            oa_view_dir=lock_dir,
        )

    omitted = make_snapshot(omissions=(OmissionRecord("shape", "x", "unsupported"),))
    with pytest.raises(UnsupportedObjectError):
        backend.restore_schematic(omitted, TARGET, allow_libraries=("sandbox",))
    backend.restore_schematic(
        omitted,
        TARGET,
        allow_libraries=("sandbox",),
        allow_omissions=True,
        verify_readback=False,
    )

    existing = backend.capture_schematic(TARGET)
    with pytest.raises(BridgeExecutionError, match="already exists"):
        backend.restore_schematic(source, TARGET, allow_libraries=("sandbox",))
    assert backend.snapshots[TARGET] == existing

    drifted = replace(source, placement=replace(source.placement, instances=()))
    backend.readback_hook = lambda _cellview, _snapshot: drifted
    with pytest.raises(RoundtripMismatchError):
        backend.restore_schematic(
            source,
            TARGET,
            allow_libraries=("sandbox",),
            overwrite=True,
        )
    assert backend.snapshots[TARGET] == existing


def test_fixture_backend_success_and_directory_loading(tmp_path: Path) -> None:
    source = make_snapshot()
    backend = FixtureBackend({CELLVIEW: source})
    receipt = backend.restore_schematic(
        source,
        TARGET,
        allow_libraries=("sandbox",),
    )
    assert receipt.published and receipt.readback_verified
    assert verify_roundtrip(source, backend.capture_schematic(TARGET)).ok

    snapshot_dir = write_snapshot(source, tmp_path / "snap")
    loaded_from_snapshot = FixtureBackend(tmp_path / "snap")
    assert loaded_from_snapshot.capture_schematic(CELLVIEW) == source

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    raw = raw_capture_payload()
    (raw_dir / "response.json").write_text(json.dumps(raw), encoding="utf-8")
    loaded_from_raw = FixtureBackend(raw_dir)
    assert loaded_from_raw.capture_schematic(CELLVIEW).electrical.instances[0].name == "M1"

    assert isinstance(load_backend("fixture"), FixtureBackend)
    with pytest.raises(BridgeExecutionError, match="unknown backend"):
        load_backend("no-such-backend")


def test_restore_request_carries_captured_port_order() -> None:
    source = make_snapshot()
    ordered = replace(
        source,
        manifest=replace(source.manifest, metadata={"port_order": ["out", "in"]}),
    )
    rendered = render_restore_request_skill(
        ordered,
        TARGET,
        draft_cell="amp__oagit_draft_001",
        overwrite=True,
        allow_libraries=("sandbox",),
        output_path=Path("restore.json"),
    )

    assert 'port_order ("out" "in")' in rendered


def test_restore_request_omits_port_order_when_metadata_is_absent() -> None:
    rendered = render_restore_request_skill(
        make_snapshot(),
        TARGET,
        draft_cell="amp__oagit_draft_001",
        overwrite=True,
        allow_libraries=("sandbox",),
        output_path=Path("restore.json"),
    )

    assert "port_order nil" in rendered
