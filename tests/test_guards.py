from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from oa_git.guards import (
    LockReport,
    RoundtripIssue,
    RoundtripReport,
    ensure_library_allowed,
    ensure_no_live_locks,
    ensure_no_omissions,
    parse_engineering_number,
    probe_locks,
    schematic_port_order,
    verify_roundtrip,
)
from oa_git.model import (
    CellViewRef,
    DisallowedLibraryError,
    Instance,
    InstancePlacement,
    LiveLockError,
    Manifest,
    Net,
    OmissionRecord,
    PinPlacement,
    Port,
    RoundtripMismatchError,
    SchemaValidationError,
    SchematicElectrical,
    SchematicPlacement,
    SchematicSnapshot,
    UnsupportedObjectError,
    WireLabel,
    WireSegment,
)


def _snapshot(
    *,
    params: dict[str, str | int | float | bool] | None = None,
    derived_params: dict[str, str] | None = None,
    placement: SchematicPlacement | None = None,
    omissions: tuple[OmissionRecord, ...] = (),
    omissions_count: int = 0,
    direction: str = "input",
    constraints: dict[str, object] | None = None,
) -> SchematicSnapshot:
    manifest = Manifest(
        schema="oa-git/1",
        view_type="schematic",
        cellview=CellViewRef("lib", "cell", "schematic"),
        electrical_sha256="electrical",
        placement_sha256="placement",
        full_sha256="full",
        files={},
        omissions_count=omissions_count,
    )
    electrical = SchematicElectrical(
        ports=(Port("in", direction),),
        nets=(Net("n1"),),
        instances=(
            Instance(
                name="R1",
                lib="basic",
                cell="res",
                connections={"p": "n1"},
                params=params or {"r": "2u"},
                param_types={"r": "string"},
                derived_params=derived_params or {},
                props={"sim": "yes"},
            ),
        ),
        properties={"view": "schematic"},
        constraints=constraints or {"check": {"enabled": True}},
    )
    if placement is None:
        placement = SchematicPlacement(
            instances=(InstancePlacement("R1", (1.0, 2.0)),),
            pins=(
                PinPlacement(
                    name="in",
                    net="n1",
                    direction="input",
                    lib="basic",
                    cell="iopin",
                    view="symbol",
                    xy=(0.0, 0.0),
                ),
            ),
            wires=(
                WireSegment(
                    points=((0.0, 0.0), (1.0, 2.0)),
                    net="n1",
                    labels=(WireLabel("N1", (0.5, 1.0)),),
                ),
            ),
            sheet={"width": 11, "height": 8.5},
        )
    return SchematicSnapshot(manifest, electrical, placement, omissions)


def test_probe_locks_recurses_and_separates_lock_kinds(tmp_path: Path) -> None:
    nested = tmp_path / "lib" / "cell"
    nested.mkdir(parents=True)
    (nested / "z.cdslck").write_text("lock")
    (tmp_path / "a.cdslck.123").write_text("lock")
    (nested / "a.lck").write_text("lock")
    (nested / "ignored.txt").write_text("not a lock")

    report = probe_locks(tmp_path)

    assert isinstance(report, LockReport)
    assert report.has_locks and report.has_cdslck
    assert [path.name for path in report.cdslck_files] == ["a.cdslck.123", "z.cdslck"]
    assert [path.name for path in report.lck_files] == ["a.lck"]
    assert report.to_dict()["has_locks"] is True


def test_probe_locks_missing_directory_is_empty(tmp_path: Path) -> None:
    report = probe_locks(tmp_path / "does-not-exist")
    assert report.cdslck_files == ()
    assert report.lck_files == ()
    assert not report.has_locks


def test_probe_locks_fails_closed_for_unreadable_or_non_directory_paths(
    tmp_path: Path,
) -> None:
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    unreadable.chmod(0)
    try:
        with pytest.raises(LiveLockError):
            probe_locks(unreadable)
    finally:
        unreadable.chmod(0o700)

    file_path = tmp_path / "file"
    file_path.write_text("not a directory")
    with pytest.raises(LiveLockError):
        probe_locks(file_path)


def test_probe_locks_translator_errors_are_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_walk(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("oa_git.guards.os.walk", fail_walk)
    with pytest.raises(LiveLockError):
        probe_locks(tmp_path)


def test_ensure_no_live_locks_blocks_cdslck_and_generic_lck(tmp_path: Path) -> None:
    (tmp_path / "view.cdslck").write_text("")
    with pytest.raises(LiveLockError, match="view.cdslck"):
        ensure_no_live_locks(tmp_path)

    (tmp_path / "view.cdslck").unlink()
    (tmp_path / "view.lck").write_text("")
    with pytest.raises(LiveLockError, match="view.lck"):
        ensure_no_live_locks(tmp_path)
    report = ensure_no_live_locks(tmp_path, block_on_generic_lck=False)
    assert report.lck_files


def test_ensure_library_allowed_is_fail_closed() -> None:
    ensure_library_allowed("work", ["work", "test"])
    with pytest.raises(DisallowedLibraryError):
        ensure_library_allowed("work", [])
    with pytest.raises(DisallowedLibraryError):
        ensure_library_allowed("work", None)  # type: ignore[arg-type]
    with pytest.raises(DisallowedLibraryError):
        ensure_library_allowed("other", (name for name in ["work"]))


def test_ensure_no_omissions_is_strict_unless_explicitly_allowed() -> None:
    omission = OmissionRecord("object", "x1", "unsupported")
    snapshot = _snapshot(omissions=(omission,), omissions_count=1)
    with pytest.raises(UnsupportedObjectError, match="x1"):
        ensure_no_omissions(snapshot)
    ensure_no_omissions(snapshot, allow_omissions=True)

    manifest_only = _snapshot(omissions_count=2)
    with pytest.raises(UnsupportedObjectError, match="omissions_count=2"):
        ensure_no_omissions(manifest_only)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (2, 2.0),
        (2.5, 2.5),
        ("2u", 2e-6),
        ("0.18u", 0.18e-6),
        ("1.2p", 1.2e-12),
        ("10k", 10e3),
        ("1Meg", 1e6),
        ("1m", 1e-3),
        ("1.8e-7", 1.8e-7),
    ],
)
def test_parse_engineering_number(value: object, expected: float) -> None:
    assert parse_engineering_number(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", [True, False, "", "2u+1", "nan", None, object()])
def test_parse_engineering_number_rejects_non_scalars_and_bool(value: object) -> None:
    assert parse_engineering_number(value) is None


def test_roundtrip_accepts_engineering_equivalence_and_ignores_derived_params() -> None:
    expected = _snapshot(params={"r": "2u"}, derived_params={"display": "old"})
    actual = _snapshot(params={"r": "2e-6"}, derived_params={})

    report = verify_roundtrip(expected, actual, raise_on_mismatch=False)

    assert report == RoundtripReport(True, True, True, ())
    assert report.to_dict()["issues"] == []


def test_roundtrip_numeric_parameter_equivalence_can_be_disabled() -> None:
    expected = _snapshot(params={"r": "2u"})
    actual = _snapshot(params={"r": 2e-6})

    report = verify_roundtrip(
        expected,
        actual,
        numeric_param_equiv=False,
        raise_on_mismatch=False,
    )

    assert not report.ok
    assert any("params['r']" in issue.path for issue in report.issues)


def test_roundtrip_rejects_bool_numeric_coercion_and_reports_diagnostic() -> None:
    expected = _snapshot(params={"r": True})
    actual = _snapshot(params={"r": 1})

    report = verify_roundtrip(expected, actual, raise_on_mismatch=False)

    assert not report.ok and not report.electrical_ok
    assert any("params['r']" in issue.path for issue in report.issues)
    with pytest.raises(RoundtripMismatchError, match="electrical:instances"):
        verify_roundtrip(expected, actual)


def test_roundtrip_checks_electrical_and_placement_fields_with_tolerance() -> None:
    expected = _snapshot()
    within_tolerance = replace(
        expected,
        placement=replace(
            expected.placement,
            instances=(InstancePlacement("R1", (1.00005, 2.00005)),),
        ),
    )
    assert verify_roundtrip(expected, within_tolerance, coord_tol=1e-4, raise_on_mismatch=False).ok

    mismatched = _snapshot(direction="output")
    mismatched = replace(
        mismatched,
        placement=replace(
            mismatched.placement,
            instances=(InstancePlacement("R1", (1.5, 2.0), "R90"),),
            sheet={"width": 12, "height": 8.5},
        ),
    )
    report = verify_roundtrip(expected, mismatched, raise_on_mismatch=False)
    assert not report.ok
    assert report.electrical_ok is False
    assert report.placement_ok is False
    assert {issue.domain for issue in report.issues} == {"electrical", "placement"}


def test_roundtrip_normalizes_reversed_wires_and_checks_pins_and_wire_labels() -> None:
    expected = _snapshot()
    reversed_wire = replace(
        expected.placement.wires[0],
        points=tuple(reversed(expected.placement.wires[0].points)),
    )
    reversed_actual = replace(
        expected,
        placement=replace(
            expected.placement,
            wires=(reversed_wire,),
        ),
    )
    assert verify_roundtrip(expected, reversed_actual, raise_on_mismatch=False).ok

    mismatched = replace(
        expected,
        placement=replace(
            expected.placement,
            pins=(replace(expected.placement.pins[0], lib="other"),),
            wires=(replace(expected.placement.wires[0], labels=(WireLabel("OTHER", (0.5, 1.0)),)),),
        ),
    )
    report = verify_roundtrip(expected, mismatched, raise_on_mismatch=False)
    assert not report.ok
    assert any(issue.path == "pins['in'].lib" for issue in report.issues)
    assert any(issue.path == "wires[0].labels" for issue in report.issues)


def test_roundtrip_detects_changes_to_one_of_duplicate_pins() -> None:
    first = PinPlacement("in", "n1", "input", "basic", "iopin", "symbol", (0, 0))
    second = PinPlacement("in", "n1", "input", "basic", "iopin", "symbol", (1, 0))
    expected = _snapshot(placement=SchematicPlacement(pins=(first, second)))
    actual = replace(
        expected,
        placement=replace(
            expected.placement,
            pins=(replace(first, xy=(2, 0)), second),
        ),
    )

    report = verify_roundtrip(expected, actual, raise_on_mismatch=False)

    assert not report.ok
    assert any("pins['in'][" in issue.path for issue in report.issues)


def test_roundtrip_normalizes_reversed_closed_wires() -> None:
    points = ((0, 0), (2, 0), (2, 2), (0, 0))
    expected = _snapshot(
        placement=SchematicPlacement(wires=(WireSegment(points, net="n1"),))
    )
    actual = replace(
        expected,
        placement=replace(
            expected.placement,
            wires=(WireSegment(tuple(reversed(points)), net="n1"),),
        ),
    )

    assert verify_roundtrip(expected, actual, raise_on_mismatch=False).ok


def test_roundtrip_numeric_equivalence_false_requires_exact_runtime_type() -> None:
    expected = _snapshot(params={"r": 1})
    actual = _snapshot(params={"r": 1.0})

    report = verify_roundtrip(
        expected,
        actual,
        numeric_param_equiv=False,
        raise_on_mismatch=False,
    )

    assert not report.ok


@pytest.mark.parametrize(
    ("allow_omissions", "omissions_count"),
    [(1, 0), (False, -1), (False, 0.5), (False, "0")],
)
def test_ensure_no_omissions_rejects_non_strict_types(
    allow_omissions: object, omissions_count: object
) -> None:
    snapshot = _snapshot()
    snapshot = replace(
        snapshot,
        manifest=replace(snapshot.manifest, omissions_count=omissions_count),
    )

    with pytest.raises(UnsupportedObjectError):
        ensure_no_omissions(snapshot, allow_omissions=allow_omissions)  # type: ignore[arg-type]


def test_roundtrip_can_skip_placement_but_not_electrical() -> None:
    expected = _snapshot()
    actual = replace(
        expected,
        placement=replace(expected.placement, sheet={"width": 99}),
    )
    report = verify_roundtrip(expected, actual, check_placement=False, raise_on_mismatch=False)
    assert report.ok and report.placement_ok


def test_roundtrip_issue_and_report_to_dict() -> None:
    issue = RoundtripIssue("electrical", "ports['in']", "input", "output", "changed")
    report = RoundtripReport(False, False, True, (issue,))
    assert issue.to_dict()["path"] == "ports['in']"
    assert report.to_dict()["issues"] == [issue.to_dict()]


def test_roundtrip_coalesces_collinear_horizontal_wires_with_labels() -> None:
    """Bug #3: IRUWB28_REPRO/DCO8 collinear touching wires merged by schCreateWire/schCheck."""

    lbl_a = WireLabel("OUT_A", (-13.125, 2.0625), justify="lowerLeft", orient="R0")
    lbl_b = WireLabel("OUT_B", (-11.5, 2.0625), justify="lowerLeft", orient="R0")
    expected = _snapshot(
        placement=SchematicPlacement(
            wires=(
                WireSegment(((-13.25, 2.0), (-13.0, 2.0)), net="n1", labels=(lbl_a,)),
                WireSegment(((-13.0, 2.0), (-10.0, 2.0)), net="n1", labels=(lbl_b,)),
            )
        )
    )
    actual = replace(
        expected,
        placement=SchematicPlacement(
            wires=(
                WireSegment(((-13.25, 2.0), (-10.0, 2.0)), net="n1", labels=(lbl_b, lbl_a)),
            )
        ),
    )

    report = verify_roundtrip(expected, actual, raise_on_mismatch=False)
    assert report.ok and report.placement_ok
    # Symmetric: actual split when expected coalesced also passes
    report_rev = verify_roundtrip(actual, expected, raise_on_mismatch=False)
    assert report_rev.ok and report_rev.placement_ok


def test_roundtrip_coalesces_vertical_chain_wide_bus_and_anonymous_bijection() -> None:
    """Bug #3: Vertical 3-segment chain, wide bus wire (0.0625), and remapped anonymous net."""

    def _snap_with_anon(net_name: str, wires: tuple[WireSegment, ...]) -> SchematicSnapshot:
        base = _snapshot()
        return replace(
            base,
            electrical=replace(
                base.electrical,
                ports=(),
                nets=(Net(net_name), Net("DATA<15:0>", num_bits=16)),
                instances=(
                    Instance(
                        name="R1",
                        lib="basic",
                        cell="res",
                        connections={"p": net_name, "m": "DATA<15:0>"},
                        params={"r": "2u"},
                        param_types={"r": "string"},
                    ),
                ),
            ),
            placement=SchematicPlacement(
                instances=(InstancePlacement("R1", (1.0, 2.0)),),
                wires=wires,
            ),
        )

    expected = _snap_with_anon(
        "net1",
        (
            # Reversed vertical 3-segment collinear chain on anonymous net1
            WireSegment(((5.0, 1.0), (5.0, 0.0)), net="net1"),
            WireSegment(((5.0, 2.5), (5.0, 1.0)), net="net1"),
            WireSegment(((5.0, 2.5), (5.0, 4.0)), net="net1"),
            # Wide bus collinear segments (PAM4_SERDES_VA/P4_TX width=0.0625)
            WireSegment(
                ((0.0, 10.0), (2.0, 10.0)),
                net="DATA<15:0>",
                width=0.0625,
                labels=(WireLabel("DATA<15:0>", (1.0, 10.0625)),),
            ),
            WireSegment(((2.0, 10.0), (6.0, 10.0)), net="DATA<15:0>", width=0.0625),
        ),
    )
    actual = _snap_with_anon(
        "net9",
        (
            WireSegment(((5.0, 0.0), (5.0, 4.0)), net="net9"),
            WireSegment(
                ((6.0, 10.0), (0.0, 10.0)),
                net="DATA<15:0>",
                width=0.0625,
                labels=(WireLabel("DATA<15:0>", (1.0, 10.0625)),),
            ),
        ),
    )

    report = verify_roundtrip(expected, actual, raise_on_mismatch=False)
    assert report.ok and report.electrical_ok and report.placement_ok


def test_roundtrip_does_not_coalesce_across_t_junction_or_corner() -> None:
    """Bug #3: Degree != 2 T-junctions and L-corners must NEVER be coalesced."""

    # T-junction at (1.0, 0.0): degree == 3 -> must not coalesce into (0,0)->(2,0)
    t_expected = _snapshot(
        placement=SchematicPlacement(
            wires=(
                WireSegment(((0.0, 0.0), (1.0, 0.0)), net="n1"),
                WireSegment(((1.0, 0.0), (2.0, 0.0)), net="n1"),
                WireSegment(((1.0, 0.0), (1.0, 1.0)), net="n1"),
            )
        )
    )
    t_unsplit_actual = replace(
        t_expected,
        placement=SchematicPlacement(
            wires=(
                WireSegment(((0.0, 0.0), (2.0, 0.0)), net="n1"),
                WireSegment(((1.0, 0.0), (1.0, 1.0)), net="n1"),
            )
        ),
    )
    t_report = verify_roundtrip(t_expected, t_unsplit_actual, raise_on_mismatch=False)
    assert not t_report.ok and not t_report.placement_ok
    assert any("wires[" in issue.path for issue in t_report.issues)

    # L-corner direction change at (1.0, 0.0) -> must not merge into diagonal (0,0)->(1,1)
    l_expected = _snapshot(
        placement=SchematicPlacement(
            wires=(
                WireSegment(((0.0, 0.0), (1.0, 0.0)), net="n1"),
                WireSegment(((1.0, 0.0), (1.0, 1.0)), net="n1"),
            )
        )
    )
    l_diag_actual = replace(
        l_expected,
        placement=SchematicPlacement(
            wires=(WireSegment(((0.0, 0.0), (1.0, 1.0)), net="n1"),)
        ),
    )
    l_report = verify_roundtrip(l_expected, l_diag_actual, raise_on_mismatch=False)
    assert not l_report.ok and not l_report.placement_ok


def test_roundtrip_coalescing_rejects_genuine_endpoint_width_label_and_foldback_mismatches() -> None:
    """Bug #3 Fail-Closed: genuine endpoint shift, width change, label move/drop, or foldback rejected."""

    lbl = WireLabel("N1", (-11.5, 2.0625))
    expected = _snapshot(
        placement=SchematicPlacement(
            wires=(
                WireSegment(((-13.25, 2.0), (-13.0, 2.0)), net="n1", width=0.0625),
                WireSegment(((-13.0, 2.0), (-10.0, 2.0)), net="n1", width=0.0625, labels=(lbl,)),
            )
        )
    )

    # 1. Endpoint shift
    shifted = replace(
        expected,
        placement=SchematicPlacement(
            wires=(
                WireSegment(((-13.25, 2.0), (-9.5, 2.0)), net="n1", width=0.0625, labels=(lbl,)),
            )
        ),
    )
    assert not verify_roundtrip(expected, shifted, raise_on_mismatch=False).ok

    # 2. Width change
    wrong_width = replace(
        expected,
        placement=SchematicPlacement(
            wires=(
                WireSegment(((-13.25, 2.0), (-10.0, 2.0)), net="n1", width=0.0, labels=(lbl,)),
            )
        ),
    )
    assert not verify_roundtrip(expected, wrong_width, raise_on_mismatch=False).ok

    # 3. Missing label
    missing_lbl = replace(
        expected,
        placement=SchematicPlacement(
            wires=(WireSegment(((-13.25, 2.0), (-10.0, 2.0)), net="n1", width=0.0625),)
        ),
    )
    assert not verify_roundtrip(expected, missing_lbl, raise_on_mismatch=False).ok

    # 4. Moved label
    moved_lbl = replace(
        expected,
        placement=SchematicPlacement(
            wires=(
                WireSegment(
                    ((-13.25, 2.0), (-10.0, 2.0)),
                    net="n1",
                    width=0.0625,
                    labels=(WireLabel("N1", (-12.0, 2.0625)),),
                ),
            )
        ),
    )
    assert not verify_roundtrip(expected, moved_lbl, raise_on_mismatch=False).ok

    # 5. Fold-back segments must never collapse into shorter segment
    foldback_exp = _snapshot(
        placement=SchematicPlacement(
            wires=(
                WireSegment(((0.0, 0.0), (2.0, 0.0)), net="n1"),
                WireSegment(((2.0, 0.0), (1.0, 0.0)), net="n1"),
            )
        )
    )
    foldback_short = replace(
        foldback_exp,
        placement=SchematicPlacement(
            wires=(WireSegment(((0.0, 0.0), (1.0, 0.0)), net="n1"),)
        ),
    )
    assert not verify_roundtrip(foldback_exp, foldback_short, raise_on_mismatch=False).ok


def _with_port_order(snapshot: SchematicSnapshot, order: list[str]) -> SchematicSnapshot:
    return replace(
        snapshot,
        manifest=replace(snapshot.manifest, metadata={"port_order": order}),
    )


def test_schematic_port_order_reads_and_validates_captured_metadata() -> None:
    snapshot = _snapshot()
    assert schematic_port_order(snapshot) is None
    assert schematic_port_order(_with_port_order(snapshot, ["in"])) == ("in",)

    with pytest.raises(SchemaValidationError, match="duplicate"):
        schematic_port_order(_with_port_order(snapshot, ["in", "in"]))
    with pytest.raises(SchemaValidationError, match="every schematic terminal"):
        schematic_port_order(_with_port_order(snapshot, ["other"]))
    with pytest.raises(SchemaValidationError, match="non-empty strings"):
        schematic_port_order(_with_port_order(snapshot, [""]))


def test_verify_roundtrip_detects_declared_port_order_change() -> None:
    base = _snapshot(
        placement=SchematicPlacement(instances=(InstancePlacement("R1", (1.0, 2.0)),))
    )
    base = replace(
        base,
        electrical=replace(
            base.electrical,
            ports=(Port("a", "input"), Port("b", "output")),
        ),
    )
    ordered = _with_port_order(base, ["a", "b"])
    reordered = _with_port_order(base, ["b", "a"])

    assert verify_roundtrip(ordered, reordered, raise_on_mismatch=False).ok is False
    with pytest.raises(RoundtripMismatchError, match="port order"):
        verify_roundtrip(ordered, reordered)
    assert verify_roundtrip(ordered, ordered).ok is True
