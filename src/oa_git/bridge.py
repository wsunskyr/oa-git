"""Backend seam and SKILL wire codec for :mod:`oa_git`.

The public backend contract deliberately keeps Virtuoso-specific execution out of
the pure Python snapshot and guard modules.  ``FixtureBackend`` provides the same
contract for deterministic offline tests, while ``load_backend`` is the only place
that knows how to discover the optional Phase 2 Virtuoso implementation.
"""

from __future__ import annotations

import importlib
import inspect
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .guards import (
    ensure_library_allowed,
    ensure_no_live_locks,
    ensure_no_omissions,
    schematic_port_order,
    verify_roundtrip,
)
from .layout_model import (
    LayoutManifest,
    LayoutRestoreReceipt,
    LayoutSnapshot,
)
from .model import (
    VALID_DERIVED_STATUSES,
    AnnotationShape,
    BridgeExecutionError,
    CdfCouplingRule,
    CdfParameterError,
    CellViewRef,
    DerivedState,
    DisallowedLibraryError,
    FailClosedError,
    Instance,
    InstancePlacement,
    LiveLockError,
    Manifest,
    Net,
    NoteLabel,
    OmissionRecord,
    PinPlacement,
    Port,
    RestoreReceipt,
    RoundtripMismatchError,
    SchemaValidationError,
    SchematicElectrical,
    SchematicPlacement,
    SchematicSnapshot,
    UnsupportedObjectError,
    WireLabel,
    WireSegment,
)
from .snapshot import (
    build_schematic_snapshot,
    compute_cdf_input_sha256,
    electrical_from_dict,
    placement_from_dict,
    read_snapshot,
    resolve_cdf_callback_order,
)
from .symbol_model import SymbolSnapshot
from .symbol_snapshot import (
    build_symbol_snapshot,
    raw_symbol_capture_to_snapshot,
    read_symbol_snapshot,
    verify_symbol_roundtrip,
)
from .view_contract import (
    BatchCaptureItem,
    BatchCaptureResult,
    OaSession,
    infer_view_type,
    read_manifest_file,
)


DEFAULT_BATCH_SKIP_LIBRARIES: tuple[str, ...] = ("basic", "analogLib", "cdsDefTechLib")


@runtime_checkable
class OaBackend(Protocol):
    """The narrow backend seam used by capture and restore callers."""

    def capture_schematic(
        self, cellview: CellViewRef, *, cds_lib: Path | None = None
    ) -> SchematicSnapshot:
        ...

    def restore_schematic(
        self,
        snapshot: SchematicSnapshot,
        target: CellViewRef,
        *,
        cds_lib: Path | None = None,
        allow_libraries: Iterable[str] = (),
        overwrite: bool = False,
        allow_omissions: bool = False,
        oa_view_dir: Path | None = None,
        verify_readback: bool = True,
        check_placement: bool = True,
    ) -> RestoreReceipt:
        ...

    def capture_batch(
        self,
        cellviews: Sequence[CellViewRef],
        *,
        cds_lib: Path | str | None = None,
        hier: bool = False,
        max_depth: int = 32,
        recompute_derived: bool = False,
        include_symbols: bool = False,
        skip_libraries: Sequence[str] = DEFAULT_BATCH_SKIP_LIBRARIES,
        session: OaSession | None = None,
    ) -> BatchCaptureResult:
        ...

    def run_skill_request(
        self,
        action: str,
        request: Mapping[str, Any],
        *,
        cds_lib: Path | str | None = None,
        session: OaSession | None = None,
    ) -> Mapping[str, Any]:
        ...


_SKILL_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_?]*$")


def _schema_error(path: str, message: str) -> SchemaValidationError:
    return SchemaValidationError(f"{path}: {message}")


def _skill_string(value: str) -> str:
    """Render one SKILL string, rejecting control characters with no safe form."""

    escaped: list[str] = []
    for character in value:
        if character == "\\":
            escaped.append("\\\\")
        elif character == '"':
            escaped.append('\\"')
        elif character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif character == "\t":
            escaped.append("\\t")
        elif ord(character) < 0x20 or ord(character) == 0x7F:
            raise SchemaValidationError(
                f"SKILL strings cannot encode control character U+{ord(character):04X}"
            )
        else:
            escaped.append(character)
    return '"' + "".join(escaped) + '"'


def _skill_symbol_or_string(value: str) -> str:
    if _SKILL_SYMBOL_RE.fullmatch(value):
        return value
    return _skill_string(value)


def _to_skill_literal(value: Any, active: set[int]) -> str:
    if value is None:
        return "nil"
    if type(value) is bool:
        return "t" if value else "nil"
    if type(value) is int:
        return str(value)
    if type(value) is float:
        if not math.isfinite(value):
            raise SchemaValidationError("SKILL numeric literals must be finite")
        return repr(value)
    if type(value) is str:
        return _skill_string(value)

    if type(value) in (list, tuple):
        object_id = id(value)
        if object_id in active:
            raise SchemaValidationError("cyclic Python containers cannot be SKILL literals")
        active.add(object_id)
        try:
            if not value:
                return "nil"
            return "(" + " ".join(_to_skill_literal(item, active) for item in value) + ")"
        finally:
            active.remove(object_id)

    if type(value) is dict:
        object_id = id(value)
        if object_id in active:
            raise SchemaValidationError("cyclic Python containers cannot be SKILL literals")
        active.add(object_id)
        try:
            keys = list(value)
            if any(type(key) is not str for key in keys):
                raise SchemaValidationError("SKILL DPL keys must be strings")
            if not keys:
                return "(nil)"
            fields: list[str] = ["nil"]
            for key in sorted(keys):
                fields.extend(
                    (
                        _skill_symbol_or_string(key),
                        _to_skill_literal(value[key], active),
                    )
                )
            return "(" + " ".join(fields) + ")"
        finally:
            active.remove(object_id)

    raise SchemaValidationError(
        f"unsupported Python type for SKILL literal: {type(value).__name__}"
    )


def to_skill_literal(value: Any) -> str:
    """Convert supported Python values into strict SKILL literals.

    Python dictionaries become SKILL disembodied property lists with a leading
    ``nil`` marker.  List and tuple values become ordinary SKILL lists.  Scalar
    values are deliberately limited to the types used by the oa-git wire format.
    """

    return _to_skill_literal(value, set())


def _absolute_path(path: Path, field: str) -> Path:
    if not isinstance(path, Path):
        raise _schema_error(field, "must be a pathlib.Path")
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _schema_error(field, f"cannot resolve path: {exc}") from exc


def _skill_list(rendered_items: Iterable[str]) -> str:
    values = tuple(rendered_items)
    return "nil" if not values else "(" + " ".join(values) + ")"


def _skill_dpl(fields: Iterable[tuple[str, str]]) -> str:
    parts = ["nil"]
    for key, rendered_value in fields:
        if not _SKILL_SYMBOL_RE.fullmatch(key):
            raise SchemaValidationError(f"invalid SKILL DPL key {key!r}")
        parts.extend((key, rendered_value))
    return "(" + " ".join(parts) + ")"


def _skill_pair_list(values: Mapping[str, Any]) -> str:
    pairs = (
        "(" + _skill_symbol_or_string(key) + " " + to_skill_literal(values[key]) + ")"
        for key in sorted(values)
    )
    return _skill_list(pairs)


def _skill_typed_triple_list(
    values: Mapping[str, Any],
    value_types: Mapping[str, str],
    *,
    context: str,
) -> str:
    triples: list[str] = []
    for key in sorted(values):
        if key not in value_types:
            raise SchemaValidationError(
                f"{context} {key!r} has no type entry"
            )
        triples.append(
            "(" + " ".join(
                (
                    _skill_symbol_or_string(key),
                    to_skill_literal(values[key]),
                    _skill_symbol_or_string(value_types[key]),
                )
            ) + ")"
        )
    return _skill_list(triples)


def _skill_triple_list(values: Mapping[str, Any], param_types: Mapping[str, str]) -> str:
    return _skill_typed_triple_list(
        values,
        param_types,
        context="restore instance parameter",
    )


def _skill_property_type(value: Any) -> str:
    """Return the explicit SKILL property type for one canonical ParamValue."""

    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "int"
    if type(value) is float:
        return "float"
    if type(value) is str:
        return "string"
    raise SchemaValidationError(
        f"restore property value must be a scalar ParamValue, got {type(value).__name__}"
    )


def _skill_property_triple_list(values: Mapping[str, Any]) -> str:
    value_types = {key: _skill_property_type(value) for key, value in values.items()}
    return _skill_typed_triple_list(
        values,
        value_types,
        context="restore property",
    )


def _normalize_coupling_rules_map(
    coupling_rules: Mapping[str, Sequence[CdfCouplingRule | Mapping[str, Any]]] | None,
) -> dict[str, tuple[CdfCouplingRule, ...]]:
    if coupling_rules is None:
        return {}
    if not isinstance(coupling_rules, Mapping):
        raise _schema_error("coupling_rules", "must be a mapping or None")
    normalized: dict[str, tuple[CdfCouplingRule, ...]] = {}
    for master_key, raw_rules in coupling_rules.items():
        if type(master_key) is not str or not master_key.strip():
            raise _schema_error("coupling_rules", f"invalid master key {master_key!r}")
        seq = _require_sequence(raw_rules, f"coupling_rules[{master_key!r}]")
        parsed: list[CdfCouplingRule] = []
        for idx, item in enumerate(seq):
            if isinstance(item, CdfCouplingRule):
                parsed.append(item)
            elif isinstance(item, Mapping):
                parsed.append(CdfCouplingRule.from_dict(dict(item)))
            else:
                raise _schema_error(
                    f"coupling_rules[{master_key!r}][{idx}]",
                    "must be a CdfCouplingRule or mapping",
                )
        normalized[master_key] = tuple(parsed)
    return normalized


def _render_port(port: Port) -> str:
    return _skill_dpl(
        (
            ("name", to_skill_literal(port.name)),
            ("direction", to_skill_literal(port.direction)),
        )
    )


def _render_instance(
    instance: Instance,
    placement: InstancePlacement | None,
    *,
    param_order: Sequence[str] = (),
) -> str:
    if placement is None:
        raise SchemaValidationError(
            f"instance {instance.name!r} has no placement geometry"
        )
    return _skill_dpl(
        (
            ("name", to_skill_literal(instance.name)),
            ("lib", to_skill_literal(instance.lib)),
            ("cell", to_skill_literal(instance.cell)),
            ("view", to_skill_literal(instance.view)),
            ("xy", to_skill_literal(list(placement.xy))),
            ("orient", to_skill_literal(placement.orient)),
            ("connections", _skill_pair_list(instance.connections)),
            ("params", _skill_triple_list(instance.params, instance.param_types)),
            ("param_order", to_skill_literal(list(param_order))),
            ("props", _skill_property_triple_list(instance.props)),
        )
    )


def _render_pin(pin: PinPlacement) -> str:
    return _skill_dpl(
        (
            ("name", to_skill_literal(pin.name)),
            ("net", to_skill_literal(pin.net)),
            ("direction", to_skill_literal(pin.direction)),
            ("lib", to_skill_literal(pin.lib)),
            ("cell", to_skill_literal(pin.cell)),
            ("view", to_skill_literal(pin.view)),
            ("xy", to_skill_literal(list(pin.xy))),
            ("orient", to_skill_literal(pin.orient)),
        )
    )


def _render_wire_label(label: WireLabel) -> str:
    return _skill_dpl(
        (
            ("text", to_skill_literal(label.text)),
            ("xy", to_skill_literal(list(label.xy))),
            ("justify", to_skill_literal(label.justify)),
            ("orient", to_skill_literal(label.orient)),
            ("font", to_skill_literal(label.font)),
            ("height", to_skill_literal(label.height)),
        )
    )


def _render_wire(wire: WireSegment) -> str:
    return _skill_dpl(
        (
            ("points", to_skill_literal([list(point) for point in wire.points])),
            ("net", to_skill_literal(wire.net)),
            ("width", to_skill_literal(wire.width)),
            ("route_method", to_skill_literal(wire.route_method)),
            ("labels", _skill_list(_render_wire_label(label) for label in wire.labels)),
        )
    )


def _render_note_label(label: NoteLabel) -> str:
    return _skill_dpl(
        (
            ("text", to_skill_literal(label.text)),
            ("xy", to_skill_literal(list(label.xy))),
            ("layer", to_skill_literal(label.layer)),
            ("purpose", to_skill_literal(label.purpose)),
            ("justify", to_skill_literal(label.justify)),
            ("orient", to_skill_literal(label.orient)),
            ("font", to_skill_literal(label.font)),
            ("height", to_skill_literal(label.height)),
        )
    )


def _render_annotation(shape: AnnotationShape) -> str:
    return _skill_dpl(
        (
            ("kind", to_skill_literal(shape.kind)),
            ("layer", to_skill_literal(shape.layer)),
            ("purpose", to_skill_literal(shape.purpose)),
            ("points", to_skill_literal([list(point) for point in shape.points])),
            ("width", to_skill_literal(shape.width)),
        )
    )


def render_capture_request_skill(cellview: CellViewRef, output_path: Path) -> str:
    """Render the complete SKILL request file for a schematic capture."""

    if not isinstance(cellview, CellViewRef):
        raise _schema_error("cellview", "must be a CellViewRef")
    if cellview.view != "schematic":
        raise _schema_error("cellview.view", "must be 'schematic'")
    response_path = _absolute_path(output_path, "output_path")
    request = _skill_dpl(
        (
            ("action", to_skill_literal("capture_schematic")),
            ("library", to_skill_literal(cellview.library)),
            ("cell", to_skill_literal(cellview.cell)),
            ("view", to_skill_literal(cellview.view)),
            ("output_path", to_skill_literal(str(response_path))),
        )
    )
    return "setq(oagitRequest '" + request + ")\n"


def render_restore_request_skill(
    snapshot: SchematicSnapshot,
    target: CellViewRef,
    *,
    draft_cell: str,
    overwrite: bool,
    allow_libraries: Iterable[str],
    output_path: Path,
    max_passes: int = 3,
    recompute_derived: bool = False,
    coupling_rules: Mapping[str, Sequence[CdfCouplingRule]] | None = None,
) -> str:
    """Render a restore request while retaining typed CDF, callback order, and geometry fields."""

    if not isinstance(snapshot, SchematicSnapshot):
        raise _schema_error("snapshot", "must be a SchematicSnapshot")
    if not isinstance(snapshot.manifest, Manifest):
        raise _schema_error("snapshot.manifest", "must be a Manifest")
    snapshot = build_schematic_snapshot(
        snapshot.manifest.cellview,
        snapshot.electrical,
        snapshot.placement,
        omissions=snapshot.omissions,
        metadata=snapshot.manifest.metadata,
    )
    if not isinstance(target, CellViewRef):
        raise _schema_error("target", "must be a CellViewRef")
    if target.view != "schematic":
        raise _schema_error("target.view", "must be 'schematic'")
    if not isinstance(draft_cell, str) or not draft_cell.strip() or "/" in draft_cell or "\\" in draft_cell:
        raise _schema_error("draft_cell", "must be a non-empty cell name")
    if type(overwrite) is not bool:
        raise _schema_error("overwrite", "must be a bool")
    if type(max_passes) is not int or max_passes < 1:
        raise _schema_error("max_passes", "must be a positive int")
    if type(recompute_derived) is not bool:
        raise _schema_error("recompute_derived", "must be a bool")
    try:
        libraries = tuple(allow_libraries)
    except TypeError as exc:
        raise _schema_error("allow_libraries", "must be iterable") from exc
    if any(type(value) is not str or not value.strip() for value in libraries):
        raise _schema_error("allow_libraries", "must contain non-empty strings")
    response_path = _absolute_path(output_path, "output_path")
    rules_map = _normalize_coupling_rules_map(coupling_rules)

    port_order = schematic_port_order(snapshot)
    instance_placements = {item.name: item for item in snapshot.placement.instances}
    ports = _skill_list(_render_port(item) for item in snapshot.electrical.ports)
    rendered_instances: list[str] = []
    for inst in snapshot.electrical.instances:
        master_key = f"{inst.lib}/{inst.cell}"
        rules_for_master = rules_map.get(master_key, ())
        order = resolve_cdf_callback_order(
            inst.params.keys(),
            declaration_order=inst.effective_derived.callback_order,
            coupling_rules=rules_for_master,
        )
        rendered_instances.append(
            _render_instance(
                inst,
                instance_placements.get(inst.name),
                param_order=order,
            )
        )
    instances = _skill_list(rendered_instances)
    pins = _skill_list(_render_pin(item) for item in snapshot.placement.pins)
    wires = _skill_list(_render_wire(item) for item in snapshot.placement.wires)
    labels = _skill_list(_render_note_label(item) for item in snapshot.placement.labels)
    annotations = _skill_list(_render_annotation(item) for item in snapshot.placement.annotations)
    properties = _skill_property_triple_list(snapshot.electrical.properties)
    request = _skill_dpl(
        (
            ("action", to_skill_literal("restore_schematic")),
            ("target", _skill_dpl(
                (
                    ("library", to_skill_literal(target.library)),
                    ("cell", to_skill_literal(target.cell)),
                    ("view", to_skill_literal(target.view)),
                )
            )),
            ("draft_cell", to_skill_literal(draft_cell)),
            ("overwrite", to_skill_literal(overwrite)),
            ("allow_libraries", to_skill_literal(list(libraries))),
            ("max_passes", to_skill_literal(max_passes)),
            ("recompute_derived", to_skill_literal(recompute_derived)),
            ("output_path", to_skill_literal(str(response_path))),
            (
                "port_order",
                _skill_list(to_skill_literal(name) for name in port_order)
                if port_order
                else "nil",
            ),
            ("ports", ports),
            ("instances", instances),
            ("pins", pins),
            ("wires", wires),
            ("labels", labels),
            ("annotations", annotations),
            ("properties", properties),
        )
    )
    return "setq(oagitRequest '" + request + ")\n"


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _schema_error(path, "must be a mapping")
    if any(type(key) is not str for key in value):
        raise _schema_error(path, "keys must be strings")
    return value


def _require_sequence(value: Any, path: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _schema_error(path, "must be a sequence")
    return value


def _require_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise _schema_error(path, "must be a bool")
    return value


def _require_text(value: Any, path: str, *, nonempty: bool = True) -> str:
    if type(value) is not str or (nonempty and not value.strip()):
        raise _schema_error(path, "must be a non-empty string" if nonempty else "must be a string")
    return value


def _require_int(value: Any, path: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise _schema_error(path, f"must be an int >= {minimum}")
    return value


def _cellview_from_payload(value: Any, path: str) -> CellViewRef:
    mapping = _require_mapping(value, path)
    expected = {"library", "cell", "view"}
    if set(mapping) != expected:
        raise _schema_error(path, f"must contain exactly {sorted(expected)!r}")
    return CellViewRef(
        library=_require_text(mapping["library"], f"{path}.library"),
        cell=_require_text(mapping["cell"], f"{path}.cell"),
        view=_require_text(mapping["view"], f"{path}.view"),
    )


_CAPTURE_KEYS = {
    "ok",
    "error",
    "cellview",
    "modified_but_not_saved",
    "metadata",
    "ports",
    "nets",
    "instances",
    "pins",
    "wires",
    "labels",
    "annotations",
    "properties",
    "constraints",
    "sheet",
    "omissions",
}


def _validate_capture_top_level(payload: Mapping[str, Any]) -> None:
    unknown = set(payload) - _CAPTURE_KEYS
    if unknown:
        raise _schema_error("capture response", f"unsupported keys {sorted(unknown)!r}")
    for required in ("ok", "cellview", "modified_but_not_saved"):
        if required not in payload:
            raise _schema_error("capture response", f"missing {required!r}")
    _require_bool(payload["ok"], "capture response.ok")
    _require_bool(payload["modified_but_not_saved"], "capture response.modified_but_not_saved")
    if "error" in payload and payload["error"] is not None:
        _require_text(payload["error"], "capture response.error", nonempty=False)


def _normalise_capture_instance(
    raw_item: Mapping[str, Any], index: int
) -> dict[str, Any]:
    path = f"capture.instances[{index}]"
    has_derived = "derived" in raw_item
    has_derived_params = "derived_params" in raw_item
    if has_derived and has_derived_params:
        raise _schema_error(
            path, "must not declare both 'derived' and legacy 'derived_params'"
        )

    param_order: list[str] = []
    if "param_order" in raw_item and raw_item["param_order"] is not None:
        raw_order = _require_sequence(raw_item["param_order"], f"{path}.param_order")
        seen: set[str] = set()
        for order_idx, item in enumerate(raw_order):
            name = _require_text(item, f"{path}.param_order[{order_idx}]")
            if name not in seen:
                seen.add(name)
                param_order.append(name)

    inst_out: dict[str, Any] = {
        key: value
        for key, value in raw_item.items()
        if key not in {"xy", "orient", "param_order", "derived_params", "derived"}
    }

    lib = raw_item.get("lib")
    cell = raw_item.get("cell")
    view = raw_item.get("view", "symbol")
    params = raw_item.get("params", {})
    param_types = raw_item.get("param_types", {})

    def _compute_input_sha() -> str:
        return compute_cdf_input_sha256(
            _require_text(lib, f"{path}.lib"),
            _require_text(cell, f"{path}.cell"),
            _require_text(view, f"{path}.view"),
            _require_mapping(params, f"{path}.params"),
            _require_mapping(param_types, f"{path}.param_types"),
        )

    if has_derived:
        raw_derived = raw_item["derived"]
        if raw_derived is not None:
            derived_map = dict(_require_mapping(raw_derived, f"{path}.derived"))
            if param_order and not derived_map.get("callback_order"):
                derived_map["callback_order"] = list(param_order)
            if (
                derived_map.get("status") == "valid"
                and derived_map.get("input_sha256") is None
            ):
                derived_map["input_sha256"] = _compute_input_sha()
            inst_out["derived"] = derived_map
    elif has_derived_params or param_order:
        raw_derived_params = raw_item.get("derived_params", {})
        derived_values = dict(
            _require_mapping(raw_derived_params, f"{path}.derived_params")
        )
        if derived_values or param_order:
            inst_out["derived"] = {
                "status": "valid",
                "values": derived_values,
                "input_sha256": _compute_input_sha(),
                "callback_order": list(param_order),
                "source": "cdf-readback",
            }
        else:
            inst_out["derived"] = {
                "status": "unavailable",
                "values": {},
                "input_sha256": None,
                "callback_order": [],
                "source": "cdf-readback",
            }

    return inst_out


def _normalise_raw_capture_lists(payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    list_fields = ("ports", "nets", "instances", "pins", "wires", "labels", "omissions")
    for field in list_fields:
        if field not in payload:
            raise _schema_error("capture response", f"missing {field!r}")
        _require_sequence(payload[field], f"capture response.{field}")
    for field in ("metadata", "properties", "constraints", "sheet"):
        if field not in payload:
            raise _schema_error("capture response", f"missing {field!r}")
        _require_mapping(payload[field], f"capture response.{field}")

    raw_instances = [
        _require_mapping(item, f"capture.instances[{idx}]")
        for idx, item in enumerate(payload["instances"])
    ]
    electrical_data = {
        "ports": list(payload["ports"]),
        "nets": list(payload["nets"]),
        "instances": [
            _normalise_capture_instance(item, idx)
            for idx, item in enumerate(raw_instances)
        ],
        "properties": dict(payload["properties"]),
        "constraints": dict(payload["constraints"]),
    }
    placement_data: dict[str, Any] = {
        "instances": [
            {
                "name": _require_text(item.get("name"), "capture instances.name"),
                "xy": item.get("xy"),
                "orient": item.get("orient", "R0"),
            }
            for item in raw_instances
        ],
        "pins": list(payload["pins"]),
        "wires": list(payload["wires"]),
        "labels": list(payload["labels"]),
        "sheet": dict(payload["sheet"]),
    }
    if "annotations" in payload and payload["annotations"] is not None:
        placement_data["annotations"] = list(
            _require_sequence(payload["annotations"], "capture response.annotations")
        )
    return electrical_data, placement_data


def raw_capture_to_snapshot(payload: Mapping[str, Any]) -> SchematicSnapshot:
    """Validate an export response and convert it to a canonical snapshot."""

    data = _require_mapping(payload, "capture response")
    if "ok" in data and not data["ok"]:
        detail = data.get("error") or "capture backend returned ok=false"
        raise BridgeExecutionError(str(detail))
    if data.get("modified_but_not_saved") is True:
        raise FailClosedError("capture response reports modified_but_not_saved=true")
    _validate_capture_top_level(data)

    cellview = _cellview_from_payload(data["cellview"], "capture response.cellview")
    if cellview.view != "schematic":
        raise _schema_error("capture response.cellview.view", "must be 'schematic'")
    electrical_data, placement_data = _normalise_raw_capture_lists(data)
    # The strict canonical parsers own all object-level validation, including CDF
    # scalar types, geometry, orientations, annotations, and omission record shape.
    electrical = electrical_from_dict(electrical_data)
    placement = placement_from_dict(placement_data)
    omissions: list[OmissionRecord] = []
    for index, item in enumerate(data["omissions"]):
        omission = _require_mapping(item, f"capture response.omissions[{index}]")
        allowed = {"kind", "identifier", "reason", "details"}
        if set(omission) - allowed or not {"kind", "identifier", "reason"}.issubset(omission):
            raise _schema_error(f"capture response.omissions[{index}]", "invalid omission fields")
        details = _require_mapping(omission.get("details", {}), f"capture response.omissions[{index}].details")
        omissions.append(
            OmissionRecord(
                kind=_require_text(omission["kind"], f"capture response.omissions[{index}].kind"),
                identifier=_require_text(omission["identifier"], f"capture response.omissions[{index}].identifier"),
                reason=_require_text(omission["reason"], f"capture response.omissions[{index}].reason"),
                details=dict(details),
            )
        )
    metadata = dict(_require_mapping(data["metadata"], "capture response.metadata"))
    return build_schematic_snapshot(
        cellview,
        electrical,
        placement,
        omissions=tuple(omissions),
        metadata=metadata,
    )


_RESTORE_ERROR_TYPES: dict[str, type[FailClosedError]] = {
    "disallowed_library": DisallowedLibraryError,
    "live_lock": LiveLockError,
    "cdf_parameter_error": CdfParameterError,
    "cdf_order_cycle": CdfParameterError,
    "cdf_contradiction": CdfParameterError,
    "cdf_nonconvergent": CdfParameterError,
    "cdf_readback_mismatch": CdfParameterError,
    "unsupported_object": UnsupportedObjectError,
    "connectivity_mismatch": RoundtripMismatchError,
    "sch_check_failed": RoundtripMismatchError,
}

_RESTORE_KEYS = {
    "ok",
    "error",
    "error_code",
    "target",
    "published",
    "draft_cell",
    "sch_check",
    "cdf_verified",
    "readback_verified",
    "connectivity_mismatches",
    "derived_status",
    "derived_refresh",
    "cdf_passes",
    "callback_order",
    "transport",
}


def _restore_error_message(payload: Mapping[str, Any], code: Any) -> str:
    detail = payload.get("error")
    if detail is None or detail == "":
        detail = "restore backend returned ok=false"
    if code is None:
        return str(detail)
    return f"{code}: {detail}"


def _restore_sch_check(payload: Mapping[str, Any]) -> tuple[int, int]:
    value = payload.get("sch_check", {})
    mapping = _require_mapping(value, "restore response.sch_check")
    unknown = set(mapping) - {"errors", "warnings"}
    if unknown:
        raise _schema_error("restore response.sch_check", f"unsupported keys {sorted(unknown)!r}")
    errors = _require_int(mapping.get("errors", 0), "restore response.sch_check.errors")
    warnings = _require_int(mapping.get("warnings", 0), "restore response.sch_check.warnings")
    return errors, warnings


def parse_restore_response(payload: Mapping[str, Any], target: CellViewRef) -> RestoreReceipt:
    """Validate a restore response and map wire error codes to domain errors."""

    data = _require_mapping(payload, "restore response")
    unknown = set(data) - _RESTORE_KEYS
    if unknown:
        raise _schema_error("restore response", f"unsupported keys {sorted(unknown)!r}")
    if "ok" not in data:
        raise _schema_error("restore response", "missing 'ok'")
    ok = _require_bool(data["ok"], "restore response.ok")
    if not isinstance(target, CellViewRef):
        raise _schema_error("target", "must be a CellViewRef")
    if target.view != "schematic":
        raise _schema_error("target.view", "must be 'schematic'")

    if not ok:
        code = data.get("error_code")
        if code is not None and type(code) is not str:
            raise _schema_error("restore response.error_code", "must be a string or null")
        if data.get("error") is not None:
            _require_text(data["error"], "restore response.error", nonempty=False)
        error_type = _RESTORE_ERROR_TYPES.get(code, BridgeExecutionError)
        raise error_type(_restore_error_message(data, code))

    response_target = _cellview_from_payload(data.get("target"), "restore response.target")
    if response_target != target:
        raise RoundtripMismatchError(
            f"restore response target {response_target.key!r} does not match {target.key!r}"
        )
    published = _require_bool(data.get("published", False), "restore response.published")
    draft_cell = data.get("draft_cell")
    if draft_cell is not None:
        _require_text(draft_cell, "restore response.draft_cell")
    sch_errors, sch_warnings = _restore_sch_check(data)
    cdf_verified = _require_bool(data.get("cdf_verified", False), "restore response.cdf_verified")
    readback_verified = _require_bool(
        data.get("readback_verified", False), "restore response.readback_verified"
    )
    mismatches = _require_sequence(
        data.get("connectivity_mismatches", []), "restore response.connectivity_mismatches"
    )
    if mismatches:
        raise RoundtripMismatchError(
            f"restore response connectivity mismatches: {list(mismatches)!r}"
        )
    if sch_errors:
        raise RoundtripMismatchError(
            f"restore response sch_check reported {sch_errors} error(s)"
        )

    derived_status_raw = data.get("derived_status")
    derived_status: str | None = None
    if derived_status_raw is not None:
        derived_status = _require_text(derived_status_raw, "restore response.derived_status")
        if derived_status not in VALID_DERIVED_STATUSES:
            raise _schema_error(
                "restore response.derived_status",
                f"must be one of {sorted(VALID_DERIVED_STATUSES)!r}",
            )

    derived_refresh_raw = data.get("derived_refresh")
    derived_refresh: dict[str, Any] | None = None
    if derived_refresh_raw is not None:
        derived_refresh = dict(
            _require_mapping(derived_refresh_raw, "restore response.derived_refresh")
        )

    cdf_passes_raw = data.get("cdf_passes")
    cdf_passes: int | None = None
    if cdf_passes_raw is not None:
        cdf_passes = _require_int(cdf_passes_raw, "restore response.cdf_passes", minimum=0)

    callback_order_raw = data.get("callback_order")
    callback_order: dict[str, list[str]] | None = None
    if callback_order_raw is not None:
        order_map = _require_mapping(callback_order_raw, "restore response.callback_order")
        callback_order = {}
        for inst_name, raw_seq in order_map.items():
            seq = _require_sequence(raw_seq, f"restore response.callback_order.{inst_name}")
            callback_order[inst_name] = [
                _require_text(item, f"restore response.callback_order.{inst_name}[{idx}]")
                for idx, item in enumerate(seq)
            ]

    transport_raw = data.get("transport")
    transport = "one_shot"
    if transport_raw is not None:
        transport = _require_text(transport_raw, "restore response.transport")

    details: dict[str, Any] = {
        "connectivity_mismatches": list(mismatches),
    }
    if data.get("error") is not None:
        _require_text(data["error"], "restore response.error", nonempty=False)
        details["error"] = data["error"]
    if data.get("error_code") is not None:
        if type(data["error_code"]) is not str:
            raise _schema_error("restore response.error_code", "must be a string or null")
        details["error_code"] = data["error_code"]
    return RestoreReceipt(
        target=target,
        published=published,
        draft_cell=draft_cell,
        sch_check_errors=sch_errors,
        sch_check_warnings=sch_warnings,
        cdf_verified=cdf_verified,
        readback_verified=readback_verified,
        details=details,
        derived_status=derived_status,
        derived_refresh=derived_refresh,
        cdf_passes=cdf_passes,
        callback_order=callback_order,
        transport=transport,
    )


FixtureSnapshot = SchematicSnapshot | SymbolSnapshot | LayoutSnapshot


def _canonical_fixture_snapshot(snapshot: FixtureSnapshot) -> FixtureSnapshot:
    if isinstance(snapshot, SchematicSnapshot):
        if not isinstance(snapshot.manifest, Manifest):
            raise SchemaValidationError("fixture snapshot manifest must be a Manifest")
        return build_schematic_snapshot(
            snapshot.manifest.cellview,
            snapshot.electrical,
            snapshot.placement,
            omissions=snapshot.omissions,
            metadata=snapshot.manifest.metadata,
        )
    if isinstance(snapshot, SymbolSnapshot):
        if not isinstance(snapshot.manifest, Manifest):
            raise SchemaValidationError("fixture symbol snapshot manifest must be a Manifest")
        return build_symbol_snapshot(
            snapshot.cellview,
            pins=snapshot.pins,
            port_order=snapshot.port_order,
            graphics=snapshot.graphics,
            properties=snapshot.properties,
            omissions=snapshot.omissions,
            metadata=snapshot.manifest.metadata,
        )
    if isinstance(snapshot, LayoutSnapshot):
        if not isinstance(snapshot.manifest, LayoutManifest):
            raise SchemaValidationError("fixture layout snapshot manifest must be a LayoutManifest")
        from .layout_snapshot import build_layout_snapshot

        return build_layout_snapshot(
            snapshot.manifest.cellview,
            snapshot.objects,
            cell_properties=snapshot.cell_properties,
            omissions=snapshot.omissions,
            metadata=snapshot.manifest.metadata,
        )
    raise SchemaValidationError(
        "fixture value must be a SchematicSnapshot, SymbolSnapshot, or LayoutSnapshot"
    )


def _fixture_key(value: Any) -> CellViewRef:
    if isinstance(value, CellViewRef):
        return value
    if type(value) is str:
        return CellViewRef.parse(value)
    raise SchemaValidationError("fixture keys must be CellViewRef or '<lib>/<cell>/<view>' strings")


def _read_fixture_snapshot_dir(snapshot_dir: Path) -> FixtureSnapshot:
    manifest = read_manifest_file(snapshot_dir)
    if manifest.view_type == "symbol":
        return read_symbol_snapshot(snapshot_dir, verify=True)
    if manifest.view_type == "layout":
        from .layout_snapshot import read_layout_snapshot

        return read_layout_snapshot(snapshot_dir, verify=True)
    return read_snapshot(snapshot_dir, verify=True)


def _raw_payload_to_fixture_snapshot(payload: Mapping[str, Any]) -> FixtureSnapshot:
    cv = payload.get("cellview")
    if isinstance(cv, Mapping):
        view_name = cv.get("view")
        if view_name == "symbol":
            return raw_symbol_capture_to_snapshot(payload)
        if isinstance(view_name, str) and infer_view_type(view_name) == "layout":
            from .layout_backend import raw_layout_capture_to_snapshot

            return raw_layout_capture_to_snapshot(payload)
    meta = payload.get("metadata")
    if isinstance(meta, Mapping) and meta.get("cellview_type") == "maskLayout":
        from .layout_backend import raw_layout_capture_to_snapshot

        return raw_layout_capture_to_snapshot(payload)
    if "objects" in payload and "nets" not in payload and "graphics" not in payload:
        from .layout_backend import raw_layout_capture_to_snapshot

        return raw_layout_capture_to_snapshot(payload)
    if "graphics" in payload and "nets" not in payload:
        return raw_symbol_capture_to_snapshot(payload)
    return raw_capture_to_snapshot(payload)


def _load_fixture_directory(path: Path) -> dict[CellViewRef, FixtureSnapshot]:
    if path.is_file():
        try:
            if path.name == "manifest.json":
                snapshot = _read_fixture_snapshot_dir(path.parent)
            else:
                with path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                snapshot = _raw_payload_to_fixture_snapshot(payload)
        except Exception as exc:
            if isinstance(exc, (BridgeExecutionError, FailClosedError, SchemaValidationError)):
                raise
            raise BridgeExecutionError(f"unable to load fixture file {path}: {exc}") from exc
        return {snapshot.manifest.cellview: snapshot}
    if not path.exists() or not path.is_dir():
        raise BridgeExecutionError(f"fixture directory does not exist: {path}")
    candidates: dict[CellViewRef, FixtureSnapshot] = {}

    manifest_paths = sorted(path.rglob("manifest.json"))
    for manifest_path in manifest_paths:
        snapshot_dir = manifest_path.parent
        try:
            snapshot = _read_fixture_snapshot_dir(snapshot_dir)
        except Exception as exc:
            if isinstance(exc, (BridgeExecutionError, FailClosedError, SchemaValidationError)):
                raise
            raise BridgeExecutionError(f"unable to load fixture snapshot {snapshot_dir}: {exc}") from exc
        key = snapshot.manifest.cellview
        if key in candidates:
            raise SchemaValidationError(f"duplicate fixture CellViewRef {key.key}")
        candidates[key] = snapshot

    json_paths = sorted(path.rglob("*.json"))
    for json_path in json_paths:
        if json_path.name in {"manifest.json", "omissions.json"}:
            continue
        try:
            with json_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeExecutionError(f"unable to read fixture JSON {json_path}: {exc}") from exc
        if not isinstance(payload, Mapping) or "ok" not in payload or "cellview" not in payload:
            continue
        snapshot = _raw_payload_to_fixture_snapshot(payload)
        key = snapshot.manifest.cellview
        if key in candidates:
            raise SchemaValidationError(f"duplicate fixture CellViewRef {key.key}")
        candidates[key] = snapshot
    return candidates


def _invoke_failure_hook(hook: Any, operation: str, context: Mapping[str, Any]) -> None:
    if hook is None:
        return
    selected = hook
    if isinstance(hook, Mapping):
        selected = hook.get(operation)
        if selected is None:
            return
    try:
        if callable(selected):
            try:
                signature = inspect.signature(selected)
                positional = [
                    parameter
                    for parameter in signature.parameters.values()
                    if parameter.kind
                    in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
                ]
                has_varargs = any(
                    parameter.kind is parameter.VAR_POSITIONAL
                    for parameter in signature.parameters.values()
                )
            except (TypeError, ValueError):
                positional, has_varargs = [], True
            if has_varargs or len(positional) >= 2:
                result = selected(operation, context)
            elif positional:
                result = selected(operation)
            else:
                result = selected()
        else:
            result = selected
    except FailClosedError:
        raise
    except Exception as exc:
        raise BridgeExecutionError(f"fixture {operation} hook failed: {exc}") from exc
    if isinstance(result, BaseException):
        if isinstance(result, FailClosedError):
            raise result
        raise BridgeExecutionError(str(result)) from result
    if result is False:
        raise BridgeExecutionError(f"fixture {operation} hook requested failure")
    if isinstance(result, str) and result:
        raise BridgeExecutionError(result)


def _invoke_readback_hook(
    hook: Any, cellview: CellViewRef, snapshot: Any
) -> Any:
    if hook is None:
        return snapshot
    selected = hook
    if isinstance(hook, Mapping):
        selected = hook.get(cellview, hook.get(cellview.key))
        if selected is None:
            return snapshot
    try:
        if callable(selected):
            try:
                signature = inspect.signature(selected)
                positional = [
                    parameter
                    for parameter in signature.parameters.values()
                    if parameter.kind
                    in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
                ]
                has_varargs = any(
                    parameter.kind is parameter.VAR_POSITIONAL
                    for parameter in signature.parameters.values()
                )
            except (TypeError, ValueError):
                positional, has_varargs = [], True
            if has_varargs or len(positional) >= 2:
                result = selected(cellview, snapshot)
            elif positional:
                result = selected(snapshot)
            else:
                result = selected()
        else:
            result = selected
    except FailClosedError:
        raise
    except Exception as exc:
        raise BridgeExecutionError(f"fixture readback hook failed: {exc}") from exc
    if result is None:
        return snapshot
    if not isinstance(result, (SchematicSnapshot, SymbolSnapshot, LayoutSnapshot)):
        raise BridgeExecutionError(
            "fixture readback hook must return a SchematicSnapshot, SymbolSnapshot, LayoutSnapshot, or None"
        )
    return result


class FixtureBackend:
    """Deterministic in-memory or on-disk backend implementing :class:`OaBackend`."""

    def __init__(
        self,
        fixtures: Mapping[Any, FixtureSnapshot] | Path | None = None,
        *,
        fixture_path: Path | None = None,
        readback_hook: Callable[..., FixtureSnapshot | None] | Mapping[Any, Any] | None = None,
        failure_hook: Callable[..., Any] | Mapping[str, Any] | Any | None = None,
        readback_drift: Callable[..., FixtureSnapshot | None] | Mapping[Any, Any] | None = None,
        backend_failure: Callable[..., Any] | Mapping[str, Any] | Any | None = None,
    ) -> None:
        if fixtures is not None and fixture_path is not None:
            raise SchemaValidationError("provide fixtures or fixture_path, not both")
        if readback_hook is not None and readback_drift is not None:
            raise SchemaValidationError("provide readback_hook or readback_drift, not both")
        if failure_hook is not None and backend_failure is not None:
            raise SchemaValidationError("provide failure_hook or backend_failure, not both")
        source = fixture_path if fixture_path is not None else fixtures
        if isinstance(source, Path):
            loaded = _load_fixture_directory(source)
        elif source is None:
            loaded = {}
        elif isinstance(source, Mapping):
            loaded = {}
            for raw_key, raw_snapshot in source.items():
                key = _fixture_key(raw_key)
                canonical = _canonical_fixture_snapshot(raw_snapshot)
                if canonical.manifest.cellview != key:
                    raise SchemaValidationError(
                        f"fixture key {key.key} does not match snapshot cellview "
                        f"{canonical.manifest.cellview.key}"
                    )
                if key in loaded:
                    raise SchemaValidationError(f"duplicate fixture CellViewRef {key.key}")
                loaded[key] = canonical
        else:
            raise SchemaValidationError("fixtures must be a mapping, Path, or None")
        self._snapshots: dict[CellViewRef, FixtureSnapshot] = loaded
        self.readback_hook = readback_hook if readback_hook is not None else readback_drift
        self.failure_hook = failure_hook if failure_hook is not None else backend_failure
        self._draft_counter = 0

    @property
    def snapshots(self) -> Mapping[CellViewRef, FixtureSnapshot]:
        """Read-only view of the current fixture database."""

        return self._snapshots

    def capture_schematic(
        self,
        cellview: CellViewRef,
        *,
        cds_lib: Path | None = None,
        session: OaSession | None = None,
    ) -> SchematicSnapshot:
        del session
        if not isinstance(cellview, CellViewRef):
            raise SchemaValidationError("cellview must be a CellViewRef")
        if cellview.view != "schematic":
            raise SchemaValidationError("cellview.view must be 'schematic'")
        if cds_lib is not None and not isinstance(cds_lib, (Path, str)):
            raise SchemaValidationError("cds_lib must be a Path, str, or None")
        if cellview not in self._snapshots:
            raise BridgeExecutionError(f"fixture has no capture for {cellview.key}")
        snapshot = self._snapshots[cellview]
        if not isinstance(snapshot, SchematicSnapshot):
            raise BridgeExecutionError(f"fixture entry for {cellview.key} is not a SchematicSnapshot")
        _invoke_failure_hook(self.failure_hook, "capture", {"cellview": cellview})
        result = _invoke_readback_hook(self.readback_hook, cellview, snapshot)
        if not isinstance(result, SchematicSnapshot):
            raise BridgeExecutionError("fixture readback hook must return a SchematicSnapshot")
        return result

    def capture_symbol(
        self,
        cellview: CellViewRef,
        *,
        cds_lib: Path | None = None,
        session: OaSession | None = None,
    ) -> SymbolSnapshot:
        del session
        if not isinstance(cellview, CellViewRef):
            raise SchemaValidationError("cellview must be a CellViewRef")
        if cellview.view != "symbol":
            raise SchemaValidationError("cellview.view must be 'symbol'")
        if cds_lib is not None and not isinstance(cds_lib, (Path, str)):
            raise SchemaValidationError("cds_lib must be a Path, str, or None")
        if cellview not in self._snapshots:
            raise BridgeExecutionError(f"fixture has no capture for {cellview.key}")
        snapshot = self._snapshots[cellview]
        if not isinstance(snapshot, SymbolSnapshot):
            raise BridgeExecutionError(f"fixture entry for {cellview.key} is not a SymbolSnapshot")
        _invoke_failure_hook(self.failure_hook, "capture_symbol", {"cellview": cellview})
        result = _invoke_readback_hook(self.readback_hook, cellview, snapshot)
        if not isinstance(result, SymbolSnapshot):
            raise BridgeExecutionError("fixture readback hook must return a SymbolSnapshot")
        return result

    def capture_batch(
        self,
        cellviews: Sequence[CellViewRef],
        *,
        cds_lib: Path | str | None = None,
        hier: bool = False,
        max_depth: int = 32,
        recompute_derived: bool = False,
        include_symbols: bool = False,
        skip_libraries: Sequence[str] = DEFAULT_BATCH_SKIP_LIBRARIES,
        session: OaSession | None = None,
    ) -> BatchCaptureResult:
        del recompute_derived, session
        if isinstance(cellviews, (str, bytes)) or not isinstance(cellviews, Sequence) or not cellviews:
            raise SchemaValidationError("cellviews must be a non-empty sequence of CellViewRef")
        if type(hier) is not bool:
            raise SchemaValidationError("hier must be a bool")
        if type(max_depth) is not int or max_depth < 0:
            raise SchemaValidationError("max_depth must be a non-negative int")
        if type(include_symbols) is not bool:
            raise SchemaValidationError("include_symbols must be a bool")
        if cds_lib is not None and not isinstance(cds_lib, (Path, str)):
            raise SchemaValidationError("cds_lib must be a Path, str, or None")

        skip_set = set(skip_libraries)
        visited: set[CellViewRef] = set()
        active_stack: set[CellViewRef] = set()
        targets: list[CellViewRef] = []
        discovered: list[CellViewRef] = []
        cycles: list[tuple[str, ...]] = []

        def _maybe_add_symbol(lib: str, cell: str) -> None:
            sym_cv = CellViewRef(lib, cell, "symbol")
            if sym_cv in self._snapshots and sym_cv not in visited:
                visited.add(sym_cv)
                targets.append(sym_cv)
                discovered.append(sym_cv)

        def _visit_hier(cv: CellViewRef, depth: int, trail: tuple[str, ...]) -> None:
            if cv in active_stack:
                cycle = (*trail, cv.key)
                if cycle not in cycles:
                    cycles.append(cycle)
                return
            if depth > max_depth:
                raise BridgeExecutionError(
                    f"hierarchy_depth_exceeded: depth {depth} exceeded max_depth {max_depth} at {cv.key}"
                )
            if cv in visited:
                return
            visited.add(cv)
            active_stack.add(cv)
            targets.append(cv)
            if depth > 0:
                discovered.append(cv)
            if include_symbols and cv.view == "schematic":
                _maybe_add_symbol(cv.library, cv.cell)

            snap = self._snapshots.get(cv)
            if isinstance(snap, SchematicSnapshot):
                seen_children: set[tuple[str, str]] = set()
                for inst in snap.electrical.instances:
                    if inst.lib in skip_set:
                        continue
                    pair = (inst.lib, inst.cell)
                    if pair in seen_children:
                        continue
                    seen_children.add(pair)
                    child_sch = CellViewRef(inst.lib, inst.cell, "schematic")
                    if child_sch in self._snapshots:
                        _visit_hier(child_sch, depth + 1, (*trail, cv.key))
                    if include_symbols:
                        _maybe_add_symbol(inst.lib, inst.cell)

            active_stack.remove(cv)

        for idx, cv in enumerate(cellviews):
            if not isinstance(cv, CellViewRef):
                raise SchemaValidationError(f"cellviews[{idx}] must be a CellViewRef")
            if hier:
                _visit_hier(cv, 0, ())
            else:
                if cv not in visited:
                    visited.add(cv)
                    targets.append(cv)
                if include_symbols and cv.view == "schematic":
                    _maybe_add_symbol(cv.library, cv.cell)

        items: list[BatchCaptureItem] = []
        for cv in targets:
            try:
                if cv.view == "schematic":
                    snap: FixtureSnapshot = self.capture_schematic(cv, cds_lib=cds_lib)
                elif cv.view == "symbol":
                    snap = self.capture_symbol(cv, cds_lib=cds_lib)
                elif infer_view_type(cv.view) == "layout" or (
                    cv in self._snapshots and isinstance(self._snapshots[cv], LayoutSnapshot)
                ):
                    snap = self.capture_layout(cv, cds_lib=cds_lib)
                else:
                    raise BridgeExecutionError(f"unsupported view type: {cv.view!r}")
            except (BridgeExecutionError, FailClosedError, SchemaValidationError) as exc:
                items.append(
                    BatchCaptureItem(
                        cellview=cv,
                        snapshot=None,
                        skipped=False,
                        reason="capture_failed",
                        error=str(exc),
                    )
                )
            else:
                items.append(
                    BatchCaptureItem(
                        cellview=cv,
                        snapshot=snap,
                        skipped=False,
                    )
                )

        return BatchCaptureResult(
            items=tuple(items),
            discovered=tuple(discovered),
            cycles=tuple(cycles),
            session={"transport": "fixture", "daemon_id": None, "reused": False},
        )

    def restore_schematic(
        self,
        snapshot: SchematicSnapshot,
        target: CellViewRef,
        *,
        cds_lib: Path | None = None,
        allow_libraries: Iterable[str] = (),
        overwrite: bool = False,
        allow_omissions: bool = False,
        oa_view_dir: Path | None = None,
        verify_readback: bool = True,
        check_placement: bool = True,
        recompute_derived: bool = False,
        max_passes: int = 3,
        coupling_rules: Mapping[str, Sequence[CdfCouplingRule]] | None = None,
        inline_readback: bool | None = None,
        session: OaSession | None = None,
    ) -> RestoreReceipt:
        del inline_readback, session
        if not isinstance(snapshot, SchematicSnapshot):
            raise SchemaValidationError("snapshot must be a SchematicSnapshot")
        if not isinstance(target, CellViewRef):
            raise SchemaValidationError("target must be a CellViewRef")
        if target.view != "schematic":
            raise SchemaValidationError("target.view must be 'schematic'")
        if cds_lib is not None and not isinstance(cds_lib, (Path, str)):
            raise SchemaValidationError("cds_lib must be a Path, str, or None")
        for field_name, value in (
            ("overwrite", overwrite),
            ("allow_omissions", allow_omissions),
            ("verify_readback", verify_readback),
            ("check_placement", check_placement),
            ("recompute_derived", recompute_derived),
        ):
            if type(value) is not bool:
                raise SchemaValidationError(f"{field_name} must be a bool")
        if type(max_passes) is not int or max_passes < 1:
            raise SchemaValidationError("max_passes must be a positive int")
        if oa_view_dir is not None and not isinstance(oa_view_dir, Path):
            raise SchemaValidationError("oa_view_dir must be a Path or None")

        rules_map = _normalize_coupling_rules_map(coupling_rules)

        # Keep this order explicit.  It is the fail-closed contract, and it also
        # means a lock or omission blocks before any target existence/write work.
        ensure_library_allowed(target.library, allow_libraries)
        ensure_no_omissions(snapshot, allow_omissions=allow_omissions)
        if oa_view_dir is not None:
            ensure_no_live_locks(oa_view_dir)
        if target in self._snapshots and not overwrite:
            raise BridgeExecutionError(
                f"restore target already exists: {target.key}; pass overwrite=True"
            )

        # Validate deterministic callback ordering (fail closed on cdf_order_cycle).
        callback_order_map: dict[str, list[str]] = {}
        refreshed_instances: list[Instance] = []
        derived_refresh: dict[str, Any] = {}
        for inst in snapshot.electrical.instances:
            master_key = f"{inst.lib}/{inst.cell}"
            rules_for_master = rules_map.get(master_key, ())
            order = resolve_cdf_callback_order(
                inst.params.keys(),
                declaration_order=inst.effective_derived.callback_order,
                coupling_rules=rules_for_master,
            )
            if order:
                callback_order_map[inst.name] = list(order)
            if recompute_derived:
                input_sha = compute_cdf_input_sha256(
                    inst.lib,
                    inst.cell,
                    inst.view,
                    inst.params,
                    inst.param_types,
                    coupling_rules=rules_for_master,
                )
                recomputed_values = dict(inst.effective_derived.values)
                new_derived = DerivedState(
                    status="valid",
                    values=recomputed_values,
                    input_sha256=input_sha,
                    callback_order=order,
                    source="cdf-readback",
                )
                derived_refresh[inst.name] = {
                    "values": recomputed_values,
                    "input_sha256": input_sha,
                }
                refreshed_instances.append(
                    Instance(
                        name=inst.name,
                        lib=inst.lib,
                        cell=inst.cell,
                        view=inst.view,
                        connections=dict(inst.connections),
                        params=dict(inst.params),
                        param_types=dict(inst.param_types),
                        derived_params=dict(recomputed_values),
                        props=dict(inst.props),
                        derived=new_derived,
                    )
                )
            else:
                refreshed_instances.append(inst)

        _invoke_failure_hook(
            self.failure_hook,
            "restore",
            {"snapshot": snapshot, "target": target},
        )
        stored_electrical = (
            SchematicElectrical(
                ports=snapshot.electrical.ports,
                nets=snapshot.electrical.nets,
                instances=tuple(refreshed_instances),
                properties=snapshot.electrical.properties,
                constraints=snapshot.electrical.constraints,
            )
            if recompute_derived
            else snapshot.electrical
        )
        stored = build_schematic_snapshot(
            target,
            stored_electrical,
            snapshot.placement,
            omissions=snapshot.omissions,
            metadata=snapshot.manifest.metadata,
        )
        previous = self._snapshots.get(target)
        had_previous = target in self._snapshots
        draft_cell = f"{target.cell}__oagit_fixture_draft_{self._draft_counter:04d}"
        self._draft_counter += 1
        try:
            _invoke_failure_hook(
                self.failure_hook,
                "write",
                {"snapshot": snapshot, "target": target, "draft_cell": draft_cell},
            )
            self._snapshots[target] = stored
            readback_verified = False
            if verify_readback:
                _invoke_failure_hook(
                    self.failure_hook,
                    "readback",
                    {"snapshot": snapshot, "target": target},
                )
                actual = self.capture_schematic(target, cds_lib=cds_lib)
                verify_roundtrip(
                    snapshot,
                    actual,
                    check_placement=check_placement,
                )
                readback_verified = True
            return RestoreReceipt(
                target=target,
                published=True,
                draft_cell=draft_cell,
                cdf_verified=True,
                readback_verified=readback_verified,
                details={"backend": "fixture", "source": snapshot.manifest.cellview.to_dict()},
                derived_status="valid" if recompute_derived else None,
                derived_refresh=derived_refresh if recompute_derived else None,
                cdf_passes=len(snapshot.electrical.instances) if snapshot.electrical.instances else 0,
                callback_order=callback_order_map or None,
                transport="fixture",
            )
        except FailClosedError:
            if had_previous:
                self._snapshots[target] = previous  # type: ignore[assignment]
            else:
                self._snapshots.pop(target, None)
            raise
        except Exception as exc:
            if had_previous:
                self._snapshots[target] = previous  # type: ignore[assignment]
            else:
                self._snapshots.pop(target, None)
            if isinstance(exc, BridgeExecutionError):
                raise
            raise BridgeExecutionError(f"fixture restore failed for {target.key}: {exc}") from exc

    def restore_symbol(
        self,
        snapshot: SymbolSnapshot,
        target: CellViewRef,
        *,
        cds_lib: Path | None = None,
        allow_libraries: Iterable[str] = (),
        overwrite: bool = False,
        allow_omissions: bool = False,
        oa_view_dir: Path | None = None,
        verify_readback: bool = True,
        inline_readback: bool | None = None,
        session: OaSession | None = None,
    ) -> RestoreReceipt:
        del inline_readback, session
        if not isinstance(snapshot, SymbolSnapshot):
            raise SchemaValidationError("snapshot must be a SymbolSnapshot")
        if not isinstance(target, CellViewRef):
            raise SchemaValidationError("target must be a CellViewRef")
        if target.view != "symbol":
            raise SchemaValidationError("target.view must be 'symbol'")
        if cds_lib is not None and not isinstance(cds_lib, (Path, str)):
            raise SchemaValidationError("cds_lib must be a Path, str, or None")
        for field_name, value in (
            ("overwrite", overwrite),
            ("allow_omissions", allow_omissions),
            ("verify_readback", verify_readback),
        ):
            if type(value) is not bool:
                raise SchemaValidationError(f"{field_name} must be a bool")
        if oa_view_dir is not None and not isinstance(oa_view_dir, Path):
            raise SchemaValidationError("oa_view_dir must be a Path or None")

        ensure_library_allowed(target.library, allow_libraries)
        ensure_no_omissions(snapshot, allow_omissions=allow_omissions)  # type: ignore[arg-type]
        if oa_view_dir is not None:
            ensure_no_live_locks(oa_view_dir)
        if target in self._snapshots and not overwrite:
            raise BridgeExecutionError(
                f"restore target already exists: {target.key}; pass overwrite=True"
            )

        _invoke_failure_hook(
            self.failure_hook,
            "restore_symbol",
            {"snapshot": snapshot, "target": target},
        )
        stored = build_symbol_snapshot(
            target,
            pins=snapshot.pins,
            port_order=snapshot.port_order,
            graphics=snapshot.graphics,
            properties=snapshot.properties,
            omissions=snapshot.omissions,
            metadata=snapshot.manifest.metadata,
        )
        previous = self._snapshots.get(target)
        had_previous = target in self._snapshots
        draft_cell = f"{target.cell}__oagit_fixture_draft_{self._draft_counter:04d}"
        self._draft_counter += 1
        try:
            _invoke_failure_hook(
                self.failure_hook,
                "write",
                {"snapshot": snapshot, "target": target, "draft_cell": draft_cell},
            )
            self._snapshots[target] = stored
            readback_verified = False
            if verify_readback:
                _invoke_failure_hook(
                    self.failure_hook,
                    "readback",
                    {"snapshot": snapshot, "target": target},
                )
                actual = self.capture_symbol(target, cds_lib=cds_lib)
                verify_symbol_roundtrip(snapshot, actual, raise_on_mismatch=True)
                readback_verified = True
            return RestoreReceipt(
                target=target,
                published=True,
                draft_cell=draft_cell,
                cdf_verified=False,
                readback_verified=readback_verified,
                details={
                    "backend": "fixture",
                    "pin_contract_verified": True,
                    "source": snapshot.manifest.cellview.to_dict(),
                },
                transport="fixture",
            )
        except FailClosedError:
            if had_previous:
                self._snapshots[target] = previous  # type: ignore[assignment]
            else:
                self._snapshots.pop(target, None)
            raise
        except Exception as exc:
            if had_previous:
                self._snapshots[target] = previous  # type: ignore[assignment]
            else:
                self._snapshots.pop(target, None)
            if isinstance(exc, BridgeExecutionError):
                raise
            raise BridgeExecutionError(f"fixture symbol restore failed for {target.key}: {exc}") from exc

    def _make_layout_fixture_backend(self) -> Any:
        from .layout_backend import FixtureLayoutBackend

        layout_snaps = {
            cv: snap
            for cv, snap in self._snapshots.items()
            if isinstance(snap, LayoutSnapshot)
        }
        fb = FixtureLayoutBackend(
            fixtures=layout_snaps,
            readback_hook=self.readback_hook,
            failure_hook=self.failure_hook,
        )
        fb._draft_counter = self._draft_counter
        return fb

    def capture_layout(
        self,
        cellview: CellViewRef,
        *,
        cds_lib: Path | str | None = None,
        session: OaSession | None = None,
    ) -> LayoutSnapshot:
        del session
        return self._make_layout_fixture_backend().capture_layout(
            cellview, cds_lib=cds_lib
        )

    def restore_layout(
        self,
        snapshot: LayoutSnapshot,
        target: CellViewRef | None = None,
        *,
        cds_lib: Path | str | None = None,
        allow_libraries: Iterable[str] = (),
        overwrite: bool = False,
        allow_omissions: bool = False,
        oa_view_dir: Path | None = None,
        verify_readback: bool = True,
        session: OaSession | None = None,
    ) -> LayoutRestoreReceipt:
        del session
        if not isinstance(snapshot, LayoutSnapshot):
            raise SchemaValidationError("snapshot must be a LayoutSnapshot")
        effective_target = snapshot.manifest.cellview if target is None else target
        fb = self._make_layout_fixture_backend()
        receipt = fb.restore_layout(
            snapshot,
            effective_target,
            cds_lib=cds_lib,
            allow_libraries=allow_libraries,
            overwrite=overwrite,
            allow_omissions=allow_omissions,
            oa_view_dir=oa_view_dir,
            verify_readback=verify_readback,
        )
        self._draft_counter = fb._draft_counter
        self._snapshots[effective_target] = fb.snapshots[effective_target]
        return receipt

    def run_skill_request(
        self,
        action: str,
        request: Mapping[str, Any],
        *,
        cds_lib: Path | str | None = None,
        session: OaSession | None = None,
    ) -> Mapping[str, Any]:
        del cds_lib, session
        if not isinstance(action, str) or not action.strip():
            raise SchemaValidationError("action must be a non-empty string")
        if not isinstance(request, Mapping):
            raise SchemaValidationError("request must be a mapping")
        _invoke_failure_hook(
            self.failure_hook,
            action,
            {"action": action, "request": dict(request)},
        )
        return {
            "ok": False,
            "error_code": "fixture_unsupported_action",
            "error": f"FixtureBackend does not execute raw SKILL action {action!r}",
        }


def load_backend(
    name: str,
    *,
    fixture_path: Path | None = None,
    virtuoso_cmd: str | None = None,
    ssh_host: str | None = None,
    remote_workdir: str | None = None,
    timeout_s: int = 180,
    daemon_enabled: bool = False,
    daemon_idle_timeout_s: int = 3600,
    daemon_startup_timeout_s: int = 30,
    daemon_poll_ms: int = 100,
    project_root: Path | None = None,
    crash_retries: int = 1,
    inline_readback: bool = False,
) -> OaBackend:
    """Load an offline fixture backend or the optional Virtuoso backend."""

    if type(name) is not str:
        raise BridgeExecutionError("backend name must be a string")
    backend_name = name.strip().lower()
    if backend_name == "fixture":
        return FixtureBackend(fixture_path=fixture_path)
    if backend_name == "virtuoso":
        try:
            module = importlib.import_module("oa_git.virtuoso_backend")
            backend_type = getattr(module, "VirtuosoSkillBackend")
        except (ImportError, AttributeError) as exc:
            raise BridgeExecutionError(
                "Virtuoso backend is unavailable. Install or provide "
                "oa_git.virtuoso_backend before selecting backend='virtuoso'."
            ) from exc
        kwargs: dict[str, Any] = {
            "virtuoso_cmd": virtuoso_cmd,
            "ssh_host": ssh_host,
            "remote_workdir": remote_workdir,
            "timeout_s": timeout_s,
            "daemon_enabled": daemon_enabled,
            "daemon_idle_timeout_s": daemon_idle_timeout_s,
            "daemon_startup_timeout_s": daemon_startup_timeout_s,
            "daemon_poll_ms": daemon_poll_ms,
            "project_root": project_root,
            "crash_retries": crash_retries,
            "inline_readback": inline_readback,
        }
        try:
            return backend_type(**kwargs)
        except TypeError as exc:
            # Phase 2 / mock implementations may expose a smaller constructor while
            # retaining the same backend protocol.  Retry progressively.
            v1_kwargs = {
                "virtuoso_cmd": virtuoso_cmd,
                "ssh_host": ssh_host,
                "remote_workdir": remote_workdir,
                "timeout_s": timeout_s,
            }
            try:
                return backend_type(**v1_kwargs)
            except TypeError:
                try:
                    return backend_type()
                except Exception as fallback_exc:
                    raise BridgeExecutionError(
                        f"unable to initialize Virtuoso backend: {fallback_exc}"
                    ) from exc
            except Exception as fallback_exc:
                raise BridgeExecutionError(
                    f"unable to initialize Virtuoso backend: {fallback_exc}"
                ) from exc
        except Exception as exc:
            if isinstance(exc, BridgeExecutionError):
                raise
            raise BridgeExecutionError(f"unable to initialize Virtuoso backend: {exc}") from exc
    raise BridgeExecutionError(f"unknown backend {name!r}; expected 'fixture' or 'virtuoso'")


__all__ = [
    "DEFAULT_BATCH_SKIP_LIBRARIES",
    "OaBackend",
    "to_skill_literal",
    "render_capture_request_skill",
    "render_restore_request_skill",
    "raw_capture_to_snapshot",
    "parse_restore_response",
    "FixtureBackend",
    "load_backend",
]
