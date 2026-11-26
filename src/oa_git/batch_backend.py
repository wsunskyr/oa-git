"""Single-session batch capture and restore orchestration for oa-git v0.2.

Provides:
- Strict SKILL request renderers for ``capture_batch`` and ``restore_batch``
  using protocol ``oagit-batch/1``.
- Strict response parser validating ``protocol``, ``request_id``, per-item
  results, deduplicated ``discovered`` CellViewRefs, ``cycles``, ``errors``,
  and ``session`` metadata.
- Local dirty-target filtering (including hierarchical known-clean subtree
  skipping with cycle-safe traversal so clean targets never launch Virtuoso).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
import re
from typing import Any

from .bridge import to_skill_literal
from .guards import schematic_port_order
from .model import (
    BATCH_PROTOCOL_V1,
    SPOOL_PROTOCOL_V1,
    CdfCouplingRule,
    CellViewRef,
    Manifest,
    SchemaValidationError,
    SchematicSnapshot,
)
from .project_state import IndexEntry, classify_cellview_state
from .snapshot import resolve_cdf_callback_order
from .view_contract import BatchCaptureItem

DEFAULT_SKIP_LIBRARIES: tuple[str, ...] = ("basic", "analogLib", "cdsDefTechLib")
DEFAULT_MAX_HIER_DEPTH = 32

_SAFE_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require_request_id(request_id: str) -> str:
    if not isinstance(request_id, str) or not _SAFE_REQUEST_ID_RE.fullmatch(request_id):
        raise SchemaValidationError(f"invalid batch request_id: {request_id!r}")
    if ".." in request_id:
        raise SchemaValidationError(f"batch request_id must not contain '..': {request_id!r}")
    return request_id


def _require_output_path(output_path: Path) -> Path:
    if not isinstance(output_path, Path):
        raise SchemaValidationError("output_path must be a pathlib.Path")
    return output_path.expanduser().resolve(strict=False)


def _plain_skill_value(value: Any, path: str) -> Any:
    """Recursively convert nested containers into plain dict/list/scalar for to_skill_literal."""

    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, CellViewRef):
        return value.to_dict()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise SchemaValidationError(f"{path}: mapping keys must be strings")
            out[key] = _plain_skill_value(item, f"{path}.{key}")
        return out
    if isinstance(value, (list, tuple)):
        return [
            _plain_skill_value(item, f"{path}[{idx}]")
            for idx, item in enumerate(value)
        ]
    raise SchemaValidationError(
        f"{path}: unsupported type for batch SKILL literal: {type(value).__name__}"
    )


def _property_type(value: Any) -> str:
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "int"
    if type(value) is float:
        return "float"
    if type(value) is str:
        return "string"
    raise SchemaValidationError(
        f"property value must be a scalar ParamValue, got {type(value).__name__}"
    )


def _normalize_coupling_rules_map(
    coupling_rules: Mapping[str, Sequence[CdfCouplingRule | Mapping[str, Any]]] | None,
) -> dict[str, tuple[CdfCouplingRule, ...]]:
    if coupling_rules is None:
        return {}
    if not isinstance(coupling_rules, Mapping):
        raise SchemaValidationError("coupling_rules must be a mapping or None")
    normalized: dict[str, tuple[CdfCouplingRule, ...]] = {}
    for master_key, raw_rules in coupling_rules.items():
        if not isinstance(master_key, str) or not master_key.strip():
            raise SchemaValidationError(f"invalid coupling_rules master key {master_key!r}")
        if isinstance(raw_rules, (str, bytes)) or not isinstance(raw_rules, Sequence):
            raise SchemaValidationError(f"coupling_rules[{master_key!r}] must be a sequence")
        parsed: list[CdfCouplingRule] = []
        for idx, item in enumerate(raw_rules):
            if isinstance(item, CdfCouplingRule):
                parsed.append(item)
            elif isinstance(item, Mapping):
                parsed.append(CdfCouplingRule.from_dict(dict(item)))
            else:
                raise SchemaValidationError(
                    f"coupling_rules[{master_key!r}][{idx}] must be a CdfCouplingRule or mapping"
                )
        normalized[master_key] = tuple(parsed)
    return normalized


def schematic_snapshot_to_restore_item(
    snapshot: SchematicSnapshot,
    target: CellViewRef,
    *,
    draft_cell: str,
    overwrite: bool = False,
    allow_libraries: Sequence[str] = (),
    recompute_derived: bool = False,
    max_passes: int = 3,
    coupling_rules: Mapping[str, Sequence[CdfCouplingRule | Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Convert a ``SchematicSnapshot`` into a batch restore item dictionary."""

    if not isinstance(snapshot, SchematicSnapshot):
        raise SchemaValidationError("snapshot must be a SchematicSnapshot")
    if not isinstance(target, CellViewRef):
        raise SchemaValidationError("target must be a CellViewRef")
    if target.view != "schematic":
        raise SchemaValidationError("schematic restore target view must be 'schematic'")
    if not isinstance(draft_cell, str) or not draft_cell.strip() or "/" in draft_cell or "\\" in draft_cell:
        raise SchemaValidationError("draft_cell must be a non-empty cell name")
    if type(overwrite) is not bool:
        raise SchemaValidationError("overwrite must be a bool")
    if type(recompute_derived) is not bool:
        raise SchemaValidationError("recompute_derived must be a bool")
    if type(max_passes) is not int or max_passes < 1:
        raise SchemaValidationError("max_passes must be a positive int")

    rules_map = _normalize_coupling_rules_map(coupling_rules)
    port_order = schematic_port_order(snapshot)
    placement_by_name = {item.name: item for item in snapshot.placement.instances}
    instances: list[dict[str, Any]] = []
    for inst in snapshot.electrical.instances:
        placement = placement_by_name.get(inst.name)
        if placement is None:
            raise SchemaValidationError(f"instance {inst.name!r} has no placement geometry")
        params_triples = []
        for k in sorted(inst.params):
            if k not in inst.param_types:
                raise SchemaValidationError(f"instance {inst.name!r} param {k!r} missing type")
            params_triples.append([k, inst.params[k], inst.param_types[k]])
        props_triples = [
            [k, inst.props[k], _property_type(inst.props[k])]
            for k in sorted(inst.props)
        ]
        order = resolve_cdf_callback_order(
            inst.params.keys(),
            declaration_order=inst.effective_derived.callback_order,
            coupling_rules=rules_map.get(f"{inst.lib}/{inst.cell}", ()),
        )
        instances.append(
            {
                "name": inst.name,
                "lib": inst.lib,
                "cell": inst.cell,
                "view": inst.view,
                "xy": list(placement.xy),
                "orient": placement.orient,
                "connections": [[k, inst.connections[k]] for k in sorted(inst.connections)],
                "params": params_triples,
                "param_order": list(order),
                "props": props_triples,
            }
        )

    return {
        "target": target.to_dict(),
        "draft_cell": draft_cell,
        "overwrite": overwrite,
        "allow_libraries": [str(lib) for lib in allow_libraries],
        "max_passes": max_passes,
        "recompute_derived": recompute_derived,
        "port_order": list(port_order) if port_order else [],
        "ports": [
            {"name": p.name, "direction": p.direction}
            for p in snapshot.electrical.ports
        ],
        "instances": instances,
        "pins": [
            {
                "name": pin.name,
                "net": pin.net,
                "direction": pin.direction,
                "lib": pin.lib,
                "cell": pin.cell,
                "view": pin.view,
                "xy": list(pin.xy),
                "orient": pin.orient,
            }
            for pin in snapshot.placement.pins
        ],
        "wires": [
            {
                "points": [list(pt) for pt in wire.points],
                "net": wire.net,
                "width": wire.width,
                "route_method": wire.route_method,
                "labels": [
                    {
                        "text": lbl.text,
                        "xy": list(lbl.xy),
                        "justify": lbl.justify,
                        "orient": lbl.orient,
                        "font": lbl.font,
                        "height": lbl.height,
                    }
                    for lbl in wire.labels
                ],
            }
            for wire in snapshot.placement.wires
        ],
        "labels": [
            {
                "text": lbl.text,
                "xy": list(lbl.xy),
                "layer": lbl.layer,
                "purpose": lbl.purpose,
                "justify": lbl.justify,
                "orient": lbl.orient,
                "font": lbl.font,
                "height": lbl.height,
            }
            for lbl in snapshot.placement.labels
        ],
        "annotations": [
            {
                "kind": shape.kind,
                "layer": shape.layer,
                "purpose": shape.purpose,
                "points": [list(pt) for pt in shape.points],
                "width": shape.width,
            }
            for shape in snapshot.placement.annotations
        ],
        "properties": [
            [k, snapshot.electrical.properties[k], _property_type(snapshot.electrical.properties[k])]
            for k in sorted(snapshot.electrical.properties)
        ],
    }


# ---------------------------------------------------------------------------
# Batch SKILL Request Renderers
# ---------------------------------------------------------------------------


def render_batch_capture_request_skill(
    cellviews: Sequence[CellViewRef],
    *,
    request_id: str,
    output_path: Path,
    hier: bool = False,
    max_depth: int = DEFAULT_MAX_HIER_DEPTH,
    recompute_derived: bool = False,
    include_symbols: bool = False,
    skip_libraries: Sequence[str] = DEFAULT_SKIP_LIBRARIES,
) -> str:
    """Render an ``oagit-batch/1`` SKILL request for ``capture_batch``."""

    if isinstance(cellviews, (str, bytes)) or not isinstance(cellviews, Sequence) or not cellviews:
        raise SchemaValidationError("cellviews must be a non-empty sequence of CellViewRef")
    for idx, cv in enumerate(cellviews):
        if not isinstance(cv, CellViewRef):
            raise SchemaValidationError(f"cellviews[{idx}] must be a CellViewRef")

    req_id = _require_request_id(request_id)
    out_path = _require_output_path(output_path)

    if type(hier) is not bool:
        raise SchemaValidationError("hier must be a bool")
    if type(max_depth) is not int or max_depth < 0:
        raise SchemaValidationError("max_depth must be a non-negative int")
    if type(recompute_derived) is not bool:
        raise SchemaValidationError("recompute_derived must be a bool")
    if type(include_symbols) is not bool:
        raise SchemaValidationError("include_symbols must be a bool")
    if isinstance(skip_libraries, (str, bytes)) or not isinstance(skip_libraries, Sequence):
        raise SchemaValidationError("skip_libraries must be a sequence of library strings")
    skip_libs_list: list[str] = []
    for lib in skip_libraries:
        if not isinstance(lib, str) or not lib.strip():
            raise SchemaValidationError("skip_libraries entries must be non-empty strings")
        skip_libs_list.append(lib)

    payload = {
        "protocol": BATCH_PROTOCOL_V1,
        "request_id": req_id,
        "action": "capture_batch",
        "cellviews": [cv.to_dict() for cv in cellviews],
        "options": {
            "hier": hier,
            "max_depth": max_depth,
            "recompute_derived": recompute_derived,
            "include_symbols": include_symbols,
            "skip_libraries": skip_libs_list,
        },
        "output_path": str(out_path),
        "output_dir": str(out_path.parent),
    }
    return "setq(oagitRequest '" + to_skill_literal(payload) + ")\n"


def render_batch_restore_request_skill(
    items: Sequence[Mapping[str, Any]],
    *,
    request_id: str,
    output_path: Path,
    readback: bool = True,
) -> str:
    """Render an ``oagit-batch/1`` SKILL request for ``restore_batch``."""

    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence) or not items:
        raise SchemaValidationError("items must be a non-empty sequence of restore item mappings")
    req_id = _require_request_id(request_id)
    out_path = _require_output_path(output_path)
    if type(readback) is not bool:
        raise SchemaValidationError("readback must be a bool")

    normalized_items: list[dict[str, Any]] = []
    for idx, raw_item in enumerate(items):
        if not isinstance(raw_item, Mapping):
            raise SchemaValidationError(f"items[{idx}] must be a mapping")
        target_raw = raw_item.get("target")
        if isinstance(target_raw, CellViewRef):
            target = target_raw
        elif isinstance(target_raw, Mapping):
            target = CellViewRef.from_dict(dict(target_raw))
        else:
            raise SchemaValidationError(f"items[{idx}].target must be a CellViewRef or mapping")

        draft_cell = raw_item.get("draft_cell")
        if (
            not isinstance(draft_cell, str)
            or not draft_cell.strip()
            or "/" in draft_cell
            or "\\" in draft_cell
        ):
            raise SchemaValidationError(f"items[{idx}].draft_cell must be a non-empty cell name")

        allow_libs = raw_item.get("allow_libraries", ())
        if isinstance(allow_libs, (str, bytes)) or not isinstance(allow_libs, Sequence):
            raise SchemaValidationError(f"items[{idx}].allow_libraries must be a sequence")
        if any(not isinstance(v, str) or not v.strip() for v in allow_libs):
            raise SchemaValidationError(
                f"items[{idx}].allow_libraries must contain non-empty strings"
            )

        overwrite = raw_item.get("overwrite", False)
        if type(overwrite) is not bool:
            raise SchemaValidationError(f"items[{idx}].overwrite must be a bool")

        plain_item = _plain_skill_value(dict(raw_item), f"items[{idx}]")
        plain_item["target"] = target.to_dict()
        plain_item["draft_cell"] = draft_cell
        plain_item["overwrite"] = overwrite
        plain_item["allow_libraries"] = [str(v) for v in allow_libs]
        normalized_items.append(plain_item)

    payload = {
        "protocol": BATCH_PROTOCOL_V1,
        "request_id": req_id,
        "action": "restore_batch",
        "items": normalized_items,
        "options": {
            "readback": readback,
        },
        "output_path": str(out_path),
        "output_dir": str(out_path.parent),
    }
    return "setq(oagitRequest '" + to_skill_literal(payload) + ")\n"


# ---------------------------------------------------------------------------
# Batch Response Parsers
# ---------------------------------------------------------------------------


def _parse_cellview_entry(raw: Any, path: str) -> CellViewRef:
    if isinstance(raw, CellViewRef):
        return raw
    if not isinstance(raw, Mapping):
        raise SchemaValidationError(f"{path}: cellview must be a mapping")
    for field in ("library", "cell", "view"):
        if field not in raw:
            raise SchemaValidationError(f"{path}: missing cellview.{field!r}")
    return CellViewRef.from_dict(dict(raw))


def _normalize_batch_errors(raw_errors: Any, label: str) -> list[dict[str, Any]]:
    if isinstance(raw_errors, (str, bytes)) or not isinstance(raw_errors, Sequence):
        raise SchemaValidationError(f"{label} 'errors' must be a list")
    errors: list[dict[str, Any]] = []
    for idx, err in enumerate(raw_errors):
        if isinstance(err, Mapping):
            errors.append(dict(err))
        elif isinstance(err, str):
            errors.append({"code": "batch_error", "message": err})
        else:
            raise SchemaValidationError(f"errors[{idx}] must be a mapping or string")
    return errors


def _validate_top_level_error_fields(
    payload: Mapping[str, Any], label: str
) -> tuple[str | None, str | None]:
    err_msg = payload.get("error")
    err_code = payload.get("error_code")
    if err_msg is not None and not isinstance(err_msg, str):
        raise SchemaValidationError(f"{label} 'error' must be a string or null")
    if err_code is not None and not isinstance(err_code, str):
        raise SchemaValidationError(f"{label} 'error_code' must be a string or null")
    return err_code, err_msg


def parse_batch_capture_response(
    payload: Mapping[str, Any],
    *,
    expected_request_id: str | None = None,
) -> dict[str, Any]:
    """Validate and normalize an ``oagit-batch/1`` capture response."""

    if not isinstance(payload, Mapping):
        raise SchemaValidationError("batch capture response must be a mapping")

    ok = payload.get("ok")
    if type(ok) is not bool:
        raise SchemaValidationError("batch capture response 'ok' must be a bool")

    protocol = payload.get("protocol")
    if protocol is None:
        if ok:
            raise SchemaValidationError(
                f"unsupported batch response protocol {protocol!r}; expected {BATCH_PROTOCOL_V1!r}"
            )
        protocol = BATCH_PROTOCOL_V1
    elif protocol != BATCH_PROTOCOL_V1:
        if not (ok is False and protocol == SPOOL_PROTOCOL_V1 and "results" not in payload):
            raise SchemaValidationError(
                f"unsupported batch response protocol {protocol!r}; expected {BATCH_PROTOCOL_V1!r}"
            )

    request_id = payload.get("request_id")
    if request_id is None:
        if ok:
            raise SchemaValidationError("batch capture response missing non-empty 'request_id'")
        request_id = expected_request_id or ""
    else:
        if not isinstance(request_id, str) or not request_id.strip():
            raise SchemaValidationError("batch capture response missing non-empty 'request_id'")
        if expected_request_id is not None and request_id != expected_request_id:
            raise SchemaValidationError(
                f"batch capture response request_id mismatch: expected {expected_request_id!r}, got {request_id!r}"
            )

    top_error_code, top_error_msg = _validate_top_level_error_fields(
        payload, "batch capture response"
    )

    # Handle top-level fatal failure (ok=False without 'results') BEFORE
    # requiring 'results' so real error_code/error messages are never masked.
    if not ok and "results" not in payload:
        errors = _normalize_batch_errors(
            payload.get("errors", ()), "batch capture response"
        )
        if not errors and (top_error_code is not None or top_error_msg is not None):
            errors.append(
                {
                    "code": top_error_code or "batch_error",
                    "message": top_error_msg or "batch capture failed",
                }
            )
        raw_session = payload.get("session", {})
        if raw_session is None:
            raw_session = {}
        if not isinstance(raw_session, Mapping):
            raise SchemaValidationError("batch capture response 'session' must be a mapping")
        return {
            "protocol": protocol,
            "request_id": request_id,
            "ok": False,
            "error": top_error_msg,
            "error_code": top_error_code,
            "results": [],
            "discovered": [],
            "cycles": [],
            "errors": errors,
            "session": dict(raw_session),
        }

    if ok:
        for req_field in ("results", "discovered", "cycles", "errors", "session"):
            if req_field not in payload:
                raise SchemaValidationError(f"batch capture response missing {req_field!r}")
    elif "results" not in payload:
        raise SchemaValidationError("batch capture response missing 'results'")

    raw_results = payload["results"]
    if isinstance(raw_results, (str, bytes)) or not isinstance(raw_results, Sequence):
        raise SchemaValidationError("batch capture response 'results' must be a list")
    results: list[dict[str, Any]] = []
    any_item_failed = False
    for idx, item in enumerate(raw_results):
        if not isinstance(item, Mapping):
            raise SchemaValidationError(f"results[{idx}] must be a mapping")
        cv = _parse_cellview_entry(item.get("cellview"), f"results[{idx}].cellview")
        item_ok = item.get("ok")
        if type(item_ok) is not bool:
            raise SchemaValidationError(f"results[{idx}].ok must be a bool")
        if item_ok:
            capture = item.get("capture")
            if not isinstance(capture, Mapping):
                raise SchemaValidationError(
                    f"results[{idx}].capture must be a mapping when ok=true"
                )
            results.append(
                {
                    "cellview": cv,
                    "ok": True,
                    "capture": dict(capture),
                    "error": None,
                    "error_code": None,
                }
            )
        else:
            any_item_failed = True
            err_msg = item.get("error")
            err_code = item.get("error_code")
            if err_msg is not None and not isinstance(err_msg, str):
                raise SchemaValidationError(f"results[{idx}].error must be a string or null")
            if err_code is not None and not isinstance(err_code, str):
                raise SchemaValidationError(f"results[{idx}].error_code must be a string or null")
            results.append(
                {
                    "cellview": cv,
                    "ok": False,
                    "capture": None,
                    "error": err_msg or "batch item capture failed",
                    "error_code": err_code,
                }
            )

    raw_discovered = payload.get("discovered", ())
    if isinstance(raw_discovered, (str, bytes)) or not isinstance(raw_discovered, Sequence):
        raise SchemaValidationError("batch capture response 'discovered' must be a list")
    discovered: list[CellViewRef] = []
    seen_discovered: set[tuple[str, str, str]] = set()
    for idx, entry in enumerate(raw_discovered):
        cv = _parse_cellview_entry(entry, f"discovered[{idx}]")
        triple = (cv.library, cv.cell, cv.view)
        if triple not in seen_discovered:
            seen_discovered.add(triple)
            discovered.append(cv)

    raw_cycles = payload.get("cycles", ())
    if isinstance(raw_cycles, (str, bytes)) or not isinstance(raw_cycles, Sequence):
        raise SchemaValidationError("batch capture response 'cycles' must be a list")
    cycles: list[tuple[str, ...]] = []
    seen_cycles: set[tuple[str, ...]] = set()
    for idx, cycle in enumerate(raw_cycles):
        if isinstance(cycle, (str, bytes)) or not isinstance(cycle, Sequence) or not cycle:
            raise SchemaValidationError(f"cycles[{idx}] must be a non-empty list of cellview keys")
        if any(not isinstance(node, str) or not node.strip() for node in cycle):
            raise SchemaValidationError(f"cycles[{idx}] entries must be non-empty strings")
        cycle_tuple = tuple(cycle)
        if cycle_tuple not in seen_cycles:
            seen_cycles.add(cycle_tuple)
            cycles.append(cycle_tuple)

    errors = _normalize_batch_errors(payload.get("errors", ()), "batch capture response")
    if not ok and not errors and (top_error_code is not None or top_error_msg is not None):
        errors.append(
            {
                "code": top_error_code or "batch_error",
                "message": top_error_msg or "batch capture failed",
            }
        )

    raw_session = payload.get("session", {})
    if not isinstance(raw_session, Mapping):
        raise SchemaValidationError("batch capture response 'session' must be a mapping")

    if ok and (any_item_failed or errors):
        raise SchemaValidationError(
            "batch capture response claims ok=true despite failed items or non-empty errors"
        )

    out: dict[str, Any] = {
        "protocol": protocol,
        "request_id": request_id,
        "ok": ok,
        "results": results,
        "discovered": discovered,
        "cycles": cycles,
        "errors": errors,
        "session": dict(raw_session),
    }
    if not ok or "error" in payload or "error_code" in payload:
        out["error"] = top_error_msg
        out["error_code"] = top_error_code
    return out


def parse_batch_restore_response(
    payload: Mapping[str, Any],
    *,
    expected_request_id: str | None = None,
) -> dict[str, Any]:
    """Validate and normalize an ``oagit-batch/1`` restore response."""

    if not isinstance(payload, Mapping):
        raise SchemaValidationError("batch restore response must be a mapping")

    ok = payload.get("ok")
    if type(ok) is not bool:
        raise SchemaValidationError("batch restore response 'ok' must be a bool")

    protocol = payload.get("protocol")
    if protocol is None:
        if ok:
            raise SchemaValidationError(
                f"unsupported batch restore protocol {protocol!r}; "
                f"expected {BATCH_PROTOCOL_V1!r}"
            )
        protocol = BATCH_PROTOCOL_V1
    elif protocol != BATCH_PROTOCOL_V1:
        if not (ok is False and protocol == SPOOL_PROTOCOL_V1 and "results" not in payload):
            raise SchemaValidationError(
                f"unsupported batch restore protocol {protocol!r}; "
                f"expected {BATCH_PROTOCOL_V1!r}"
            )

    request_id = payload.get("request_id")
    if request_id is None:
        if ok:
            raise SchemaValidationError("batch restore response missing non-empty 'request_id'")
        request_id = expected_request_id or ""
    else:
        if not isinstance(request_id, str) or not request_id.strip():
            raise SchemaValidationError("batch restore response missing non-empty 'request_id'")
        if expected_request_id is not None and request_id != expected_request_id:
            raise SchemaValidationError(
                f"batch restore response request_id mismatch: expected {expected_request_id!r}, got {request_id!r}"
            )

    top_error_code, top_error_msg = _validate_top_level_error_fields(
        payload, "batch restore response"
    )

    # Handle top-level fatal failure (ok=False without 'results') BEFORE
    # requiring 'results' so real error_code/error messages are never masked.
    if not ok and "results" not in payload:
        raw_errors = payload.get("errors", ())
        if isinstance(raw_errors, (str, bytes)) or not isinstance(raw_errors, Sequence):
            raise SchemaValidationError("batch restore response 'errors' must be a list")
        errors_list = list(raw_errors)
        if not errors_list and (top_error_code is not None or top_error_msg is not None):
            errors_list.append(
                {
                    "code": top_error_code or "batch_error",
                    "message": top_error_msg or "batch restore failed",
                }
            )
        raw_session = payload.get("session", {})
        if raw_session is None:
            raw_session = {}
        if not isinstance(raw_session, Mapping):
            raise SchemaValidationError("batch restore response 'session' must be a mapping")
        return {
            "protocol": protocol,
            "request_id": request_id,
            "ok": False,
            "error": top_error_msg,
            "error_code": top_error_code,
            "results": [],
            "errors": errors_list,
            "session": dict(raw_session),
        }

    raw_results = payload.get("results")
    if isinstance(raw_results, (str, bytes)) or not isinstance(raw_results, Sequence):
        raise SchemaValidationError("batch restore response 'results' must be a list")
    raw_errors = payload.get("errors", [])
    if isinstance(raw_errors, (str, bytes)) or not isinstance(raw_errors, Sequence):
        raise SchemaValidationError("batch restore response 'errors' must be a list")
    raw_session = payload.get("session", {} if not ok else None)
    if not isinstance(raw_session, Mapping):
        raise SchemaValidationError("batch restore response 'session' must be a mapping")

    results: list[dict[str, Any]] = []
    any_failed = False
    for idx, item in enumerate(raw_results):
        if not isinstance(item, Mapping):
            raise SchemaValidationError(f"results[{idx}] must be a mapping")
        target = _parse_cellview_entry(item.get("target"), f"results[{idx}].target")
        item_ok = item.get("ok")
        published = item.get("published")
        if type(item_ok) is not bool:
            raise SchemaValidationError(f"results[{idx}].ok must be a bool")
        if type(published) is not bool:
            raise SchemaValidationError(f"results[{idx}].published must be a bool")
        if item_ok and not published:
            raise SchemaValidationError(
                f"results[{idx}] claims ok=true but published=false"
            )
        if not item_ok:
            any_failed = True
        readback = item.get("readback")
        if readback is not None and not isinstance(readback, Mapping):
            raise SchemaValidationError(f"results[{idx}].readback must be a mapping or null")
        parsed_item: dict[str, Any] = {
            "target": target,
            "ok": item_ok,
            "published": published,
            "draft_cell": item.get("draft_cell"),
            "sch_check": dict(item.get("sch_check") or {}),
            "cdf_verified": bool(item.get("cdf_verified", False)),
            "connectivity_mismatches": list(item.get("connectivity_mismatches") or ()),
            "readback": dict(readback) if readback is not None else None,
            "error": item.get("error"),
            "error_code": item.get("error_code"),
        }

        if "readback_verified" in item:
            rv = item["readback_verified"]
            if rv is not None and type(rv) is not bool:
                raise SchemaValidationError(
                    f"results[{idx}].readback_verified must be a bool or null"
                )
            parsed_item["readback_verified"] = rv

        if "cdf_passes" in item:
            cp = item["cdf_passes"]
            if cp is not None and (type(cp) is not int or cp < 0):
                raise SchemaValidationError(
                    f"results[{idx}].cdf_passes must be a non-negative int or null"
                )
            parsed_item["cdf_passes"] = cp

        if "callback_order" in item:
            co = item["callback_order"]
            if co is not None:
                if not isinstance(co, Mapping):
                    raise SchemaValidationError(
                        f"results[{idx}].callback_order must be a mapping or null"
                    )
                norm_co: dict[str, list[str]] = {}
                for inst_name, seq in co.items():
                    if not isinstance(inst_name, str):
                        raise SchemaValidationError(
                            f"results[{idx}].callback_order keys must be strings"
                        )
                    if isinstance(seq, (str, bytes)) or not isinstance(seq, Sequence):
                        raise SchemaValidationError(
                            f"results[{idx}].callback_order.{inst_name} must be a list"
                        )
                    norm_co[inst_name] = [str(v) for v in seq]
                parsed_item["callback_order"] = norm_co
            else:
                parsed_item["callback_order"] = None

        if "derived_status" in item:
            ds = item["derived_status"]
            if ds is not None and not isinstance(ds, str):
                raise SchemaValidationError(
                    f"results[{idx}].derived_status must be a string or null"
                )
            parsed_item["derived_status"] = ds

        if "derived_refresh" in item:
            dr = item["derived_refresh"]
            if dr is not None:
                if not isinstance(dr, Mapping):
                    raise SchemaValidationError(
                        f"results[{idx}].derived_refresh must be a mapping or null"
                    )
                parsed_item["derived_refresh"] = dict(dr)
            else:
                parsed_item["derived_refresh"] = None

        if "pin_contract_verified" in item:
            pcv = item["pin_contract_verified"]
            if pcv is not None and type(pcv) is not bool:
                raise SchemaValidationError(
                    f"results[{idx}].pin_contract_verified must be a bool or null"
                )
            parsed_item["pin_contract_verified"] = pcv

        if "pin_mismatches" in item:
            pm = item["pin_mismatches"]
            if pm is not None:
                if isinstance(pm, (str, bytes)) or not isinstance(pm, Sequence):
                    raise SchemaValidationError(
                        f"results[{idx}].pin_mismatches must be a list or null"
                    )
                parsed_item["pin_mismatches"] = list(pm)
            else:
                parsed_item["pin_mismatches"] = None

        if "symbol_check" in item:
            sc = item["symbol_check"]
            if sc is not None:
                if not isinstance(sc, Mapping):
                    raise SchemaValidationError(
                        f"results[{idx}].symbol_check must be a mapping or null"
                    )
                parsed_item["symbol_check"] = dict(sc)
            else:
                parsed_item["symbol_check"] = None

        if "pcell_verified" in item:
            pv = item["pcell_verified"]
            if pv is not None and type(pv) is not bool:
                raise SchemaValidationError(
                    f"results[{idx}].pcell_verified must be a bool or null"
                )
            parsed_item["pcell_verified"] = pv

        if "connectivity_verified" in item:
            cv_flag = item["connectivity_verified"]
            if cv_flag is not None and type(cv_flag) is not bool:
                raise SchemaValidationError(
                    f"results[{idx}].connectivity_verified must be a bool or null"
                )
            parsed_item["connectivity_verified"] = cv_flag

        if "object_counts" in item:
            oc = item["object_counts"]
            if oc is not None:
                if not isinstance(oc, Mapping):
                    raise SchemaValidationError(
                        f"results[{idx}].object_counts must be a mapping or null"
                    )
                norm_oc: dict[str, int] = {}
                for count_key, count_val in oc.items():
                    if not isinstance(count_key, str):
                        raise SchemaValidationError(
                            f"results[{idx}].object_counts keys must be strings"
                        )
                    if type(count_val) is not int or count_val < 0:
                        raise SchemaValidationError(
                            f"results[{idx}].object_counts.{count_key} must be a non-negative int"
                        )
                    norm_oc[count_key] = count_val
                parsed_item["object_counts"] = norm_oc
            else:
                parsed_item["object_counts"] = None

        if "pcell_mismatches" in item:
            pcm = item["pcell_mismatches"]
            if pcm is not None:
                if isinstance(pcm, (str, bytes)) or not isinstance(pcm, Sequence):
                    raise SchemaValidationError(
                        f"results[{idx}].pcell_mismatches must be a list or null"
                    )
                parsed_item["pcell_mismatches"] = list(pcm)
            else:
                parsed_item["pcell_mismatches"] = None

        results.append(parsed_item)

    errors_out = list(raw_errors)
    if not ok and not errors_out and (top_error_code is not None or top_error_msg is not None):
        errors_out.append(
            {
                "code": top_error_code or "batch_error",
                "message": top_error_msg or "batch restore failed",
            }
        )

    if ok and (any_failed or errors_out):
        raise SchemaValidationError(
            "batch restore response claims ok=true despite failed items or non-empty errors"
        )

    out_res: dict[str, Any] = {
        "protocol": protocol,
        "request_id": request_id,
        "ok": ok,
        "results": results,
        "errors": errors_out,
        "session": dict(raw_session),
    }
    if not ok or "error" in payload or "error_code" in payload:
        out_res["error"] = top_error_msg
        out_res["error_code"] = top_error_code
    return out_res


# ---------------------------------------------------------------------------
# Local Dirty-Target & Known-Clean Subtree Filter
# ---------------------------------------------------------------------------


def filter_dirty_targets(
    cellviews: Sequence[CellViewRef],
    *,
    oa_libs: Mapping[str, str | Path],
    snapshots_dir: Path,
    index_entries: Mapping[str, IndexEntry],
    hier: bool = False,
    include_symbols: bool = False,
    verify_snapshot_fn: Callable[[Path], Manifest] | None = None,
) -> tuple[list[CellViewRef], list[BatchCaptureItem]]:
    """Partition requested CellViews into ``(dirty_targets, skipped_items)``.

    - When ``hier=False``: any CellView whose local fingerprint & snapshot
      integrity classify as ``clean`` is skipped locally with zero Virtuoso calls
      (and when ``include_symbols=True``, a schematic's corresponding ``symbol``
      entry must also classify as ``clean`` before skipping).
    - When ``hier=True``: a root CellView is skipped locally ONLY if the root
      AND every CellView in its known transitive ``dependencies`` subtree are
      present in ``index_entries``, classify as ``clean`` (cycle-safe), AND
      every schematic entry in the subtree has ``hierarchy_complete=True`` (plus
      its corresponding ``symbol`` entry is ``clean`` when ``include_symbols=True``).
      Any incomplete hierarchy, dirty, missing, unknown, or unindexed dependency
      forces the root into ``dirty_targets`` so Virtuoso performs an authoritative
      capture.
    """

    if type(hier) is not bool:
        raise SchemaValidationError("hier must be a bool")
    if type(include_symbols) is not bool:
        raise SchemaValidationError("include_symbols must be a bool")

    state_cache: dict[str, str] = {}

    def _state_for(cv: CellViewRef) -> str:
        cached = state_cache.get(cv.key)
        if cached is not None:
            return cached
        state, _ = classify_cellview_state(
            cv,
            oa_libs=oa_libs,
            snapshots_dir=snapshots_dir,
            index_entries=index_entries,
            verify_snapshot_fn=verify_snapshot_fn,
        )
        state_cache[cv.key] = state
        return state

    def _symbol_clean_if_requested(cv: CellViewRef) -> bool:
        if not include_symbols or cv.view != "schematic":
            return True
        sym_cv = CellViewRef(cv.library, cv.cell, "symbol")
        return _state_for(sym_cv) == "clean"

    def _subtree_is_known_clean(root_cv: CellViewRef) -> bool:
        if _state_for(root_cv) != "clean":
            return False
        root_entry = index_entries.get(root_cv.key)
        if root_entry is None:
            return False
        if root_cv.view == "schematic" and not bool(
            getattr(root_entry, "hierarchy_complete", False)
        ):
            return False
        if not _symbol_clean_if_requested(root_cv):
            return False
        visited: set[str] = {root_cv.key}
        queue: list[str] = list(root_entry.dependencies)
        while queue:
            dep_key = queue.pop(0)
            if dep_key in visited:
                continue
            visited.add(dep_key)
            dep_entry = index_entries.get(dep_key)
            if dep_entry is None:
                return False
            try:
                dep_cv = CellViewRef.parse(dep_key)
            except SchemaValidationError:
                return False
            if _state_for(dep_cv) != "clean":
                return False
            if dep_cv.view == "schematic" and not bool(
                getattr(dep_entry, "hierarchy_complete", False)
            ):
                return False
            if not _symbol_clean_if_requested(dep_cv):
                return False
            queue.extend(dep_entry.dependencies)
        return True

    dirty_targets: list[CellViewRef] = []
    skipped_items: list[BatchCaptureItem] = []
    seen: set[str] = set()

    for cv in cellviews:
        if not isinstance(cv, CellViewRef):
            raise SchemaValidationError(f"expected CellViewRef, got {type(cv).__name__}")
        if cv.key in seen:
            continue
        seen.add(cv.key)

        if hier:
            is_clean = _subtree_is_known_clean(cv)
        else:
            is_clean = _state_for(cv) == "clean" and _symbol_clean_if_requested(cv)

        if is_clean:
            skipped_items.append(
                BatchCaptureItem(
                    cellview=cv,
                    snapshot=None,
                    skipped=True,
                    reason="clean",
                )
            )
        else:
            dirty_targets.append(cv)

    return dirty_targets, skipped_items


__all__ = [
    "DEFAULT_SKIP_LIBRARIES",
    "DEFAULT_MAX_HIER_DEPTH",
    "schematic_snapshot_to_restore_item",
    "render_batch_capture_request_skill",
    "render_batch_restore_request_skill",
    "parse_batch_capture_response",
    "parse_batch_restore_response",
    "filter_dirty_targets",
]
