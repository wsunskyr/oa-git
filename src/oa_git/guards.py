"""Fail-closed safety guards and schematic roundtrip verification.

The functions in this module deliberately operate on the immutable domain
objects from :mod:`oa_git.model`.  They do not read or write OpenAccess data
and therefore remain useful in offline tests and in the backend seam.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence, Set as AbstractSet
from dataclasses import asdict, dataclass, is_dataclass
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Literal

from .model import (
    AnnotationShape,
    CdfCouplingRule,
    DisallowedLibraryError,
    Instance,
    InstancePlacement,
    LiveLockError,
    NoteLabel,
    PinPlacement,
    RoundtripMismatchError,
    SchemaValidationError,
    SchematicSnapshot,
    UnsupportedObjectError,
    WireLabel,
    WireSegment,
)
from .snapshot import compute_cdf_input_sha256, resolve_cdf_callback_order


@dataclass(frozen=True)
class LockReport:
    """Locks discovered below an OA view directory."""

    target_dir: Path
    cdslck_files: tuple[Path, ...]
    lck_files: tuple[Path, ...]

    @property
    def has_locks(self) -> bool:
        return bool(self.cdslck_files or self.lck_files)

    @property
    def has_cdslck(self) -> bool:
        return bool(self.cdslck_files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_dir": str(self.target_dir),
            "cdslck_files": [str(path) for path in self.cdslck_files],
            "lck_files": [str(path) for path in self.lck_files],
            "has_locks": self.has_locks,
            "has_cdslck": self.has_cdslck,
        }


def _safe_resolve(path: Path) -> Path:
    """Resolve a path without allowing a malformed symlink to abort probing."""

    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path.absolute()


def probe_locks(oa_view_dir: Path) -> LockReport:
    """Find Cadence lock files below ``oa_view_dir``.

    The returned paths are resolved paths, which makes diagnostics independent
    of the caller's current working directory.  A missing root is valid for a
    new target, while every other unreadable state fails closed.
    """

    root = Path(oa_view_dir)
    target_dir = _safe_resolve(root)
    cdslck_files: list[Path] = []
    lck_files: list[Path] = []

    try:
        root_stat = os.lstat(root)
    except FileNotFoundError:
        return LockReport(target_dir, (), ())
    except OSError as exc:
        raise LiveLockError(f"unable to inspect OA view directory {root}: {exc}") from exc
    root_is_symlink = stat.S_ISLNK(root_stat.st_mode)

    def fail_unreadable(path: Path, detail: str) -> None:
        raise LiveLockError(f"unable to inspect OA view path {path}: {detail}")

    def ensure_directory_readable(path: Path) -> None:
        try:
            mode = path.stat().st_mode
            if not stat.S_ISDIR(mode):
                fail_unreadable(path, "path is not a directory")
            read_bits = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
            execute_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            if not mode & read_bits or not mode & execute_bits:
                fail_unreadable(path, "directory is not readable and searchable")
            with os.scandir(path):
                pass
        except LiveLockError:
            raise
        except OSError as exc:
            fail_unreadable(path, str(exc))

    def ensure_symlink_target_readable(path: Path) -> Path:
        try:
            target = path.resolve(strict=True)
            if target.is_dir():
                ensure_directory_readable(target)
            else:
                mode = target.stat().st_mode
                read_bits = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
                if not mode & read_bits:
                    fail_unreadable(target, "symlink target is not readable")
            return target
        except LiveLockError:
            raise
        except OSError as exc:
            fail_unreadable(path, str(exc))
        raise AssertionError("unreachable")  # pragma: no cover

    if root_is_symlink:
        root_target = ensure_symlink_target_readable(root)
        if not root_target.is_dir():
            fail_unreadable(root, "symlink target is not a directory")
    else:
        try:
            if not root.is_dir():
                fail_unreadable(root, "path is not a directory")
        except OSError as exc:
            fail_unreadable(root, str(exc))
    ensure_directory_readable(root)

    def raise_walk_error(error: OSError) -> None:
        raise LiveLockError(f"unable to traverse OA view directory {root}: {error}") from error

    try:
        walker = os.walk(root, topdown=True, followlinks=False, onerror=raise_walk_error)
        for current, directories, filenames in walker:
            current_path = Path(current)
            ensure_directory_readable(current_path)
            directories.sort()
            for directory in directories:
                directory_path = current_path / directory
                try:
                    if directory_path.is_symlink():
                        ensure_symlink_target_readable(directory_path)
                    elif not directory_path.is_dir():
                        fail_unreadable(directory_path, "directory disappeared during traversal")
                except LiveLockError:
                    raise
                except OSError as exc:
                    fail_unreadable(directory_path, str(exc))
            for filename in sorted(filenames):
                path = current_path / filename
                try:
                    if path.is_symlink():
                        ensure_symlink_target_readable(path)
                    if not path.is_file():
                        continue
                except LiveLockError:
                    raise
                except OSError as exc:
                    fail_unreadable(path, str(exc))

                is_cdslck = ".cdslck" in filename
                is_generic_lck = filename.endswith(".lck")
                if not is_cdslck and not is_generic_lck:
                    continue

                try:
                    resolved = path.resolve(strict=True)
                except OSError as exc:
                    fail_unreadable(path, str(exc))
                if is_cdslck:
                    cdslck_files.append(resolved)
                else:
                    lck_files.append(resolved)
    except LiveLockError:
        raise
    except OSError as exc:
        # ``os.walk`` normally reports traversal errors through ``onerror``.
        # Keep this outer guard for unusual filesystem implementations.
        raise LiveLockError(f"unable to traverse OA view directory {root}: {exc}") from exc

    return LockReport(
        target_dir=target_dir,
        cdslck_files=tuple(sorted(set(cdslck_files), key=str)),
        lck_files=tuple(sorted(set(lck_files), key=str)),
    )


def ensure_no_live_locks(
    oa_view_dir: Path, *, block_on_generic_lck: bool = True
) -> LockReport:
    """Reject an operation when blocking Cadence edit locks are present."""

    report = probe_locks(oa_view_dir)
    blocking = list(report.cdslck_files)
    if block_on_generic_lck:
        blocking.extend(report.lck_files)
    if blocking:
        lock_text = ", ".join(str(path) for path in blocking)
        raise LiveLockError(f"Live lock(s) block operation: {lock_text}")
    return report


def ensure_library_allowed(target_library: str, allow_libraries: Iterable[str]) -> None:
    """Require an explicit, non-empty destination library allowlist."""

    try:
        allowed = tuple(allow_libraries)
    except TypeError:
        # Treat a malformed or missing allowlist as empty.  The guard remains
        # fail-closed instead of leaking an incidental iteration error.
        allowed = ()
    if not allowed or target_library not in allowed:
        allowed_text = ", ".join(repr(value) for value in allowed) or "<empty>"
        raise DisallowedLibraryError(
            f"Target library {target_library!r} is not in the explicit allowlist "
            f"({allowed_text})"
        )


def ensure_no_omissions(
    snapshot: SchematicSnapshot, *, allow_omissions: bool = False
) -> None:
    """Reject snapshots whose capture recorded unsupported objects."""

    if type(allow_omissions) is not bool:
        raise UnsupportedObjectError("allow_omissions must be a bool")
    omissions = tuple(snapshot.omissions)
    omissions_count = snapshot.manifest.omissions_count
    if type(omissions_count) is not int or omissions_count < 0:
        raise UnsupportedObjectError(
            "manifest.omissions_count must be a non-negative int"
        )
    if allow_omissions or (not omissions and omissions_count <= 0):
        return

    details: list[str] = []
    if omissions:
        details.extend(
            f"{record.kind}:{record.identifier} ({record.reason})"
            for record in omissions
        )
    if omissions_count > len(omissions):
        details.append(f"manifest.omissions_count={omissions_count}")
    detail_text = "; ".join(details) or f"manifest.omissions_count={omissions_count}"
    raise UnsupportedObjectError(f"Snapshot contains omissions: {detail_text}")


_ENGINEERING_NUMBER_RE = re.compile(
    r"^[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))"
    r"(?:[eE][+-]?\d+)?(?P<suffix>Meg|meg|[TtGgKkMmUuNnPpFfAa])?$"
)
_ENGINEERING_MULTIPLIERS = {
    "T": 1e12,
    "t": 1e12,
    "G": 1e9,
    "g": 1e9,
    "Meg": 1e6,
    "meg": 1e6,
    "K": 1e3,
    "k": 1e3,
    "m": 1e-3,
    "M": 1e6,
    "U": 1e-6,
    "u": 1e-6,
    "N": 1e-9,
    "n": 1e-9,
    "P": 1e-12,
    "p": 1e-12,
    "F": 1e-15,
    "f": 1e-15,
    "A": 1e-18,
    "a": 1e-18,
}


def parse_engineering_number(value: Any) -> float | None:
    """Parse a scalar number or a SPICE/Cadence engineering value."""

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return None
        return number if math.isfinite(number) else None
    if not isinstance(value, str):
        return None

    text = value.strip()
    match = _ENGINEERING_NUMBER_RE.fullmatch(text)
    if match is None:
        return None
    suffix = match.group("suffix")
    number_text = text[: match.start("suffix")] if suffix else text
    try:
        number = float(number_text)
        if suffix:
            number *= _ENGINEERING_MULTIPLIERS[suffix]
    except (KeyError, OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class RoundtripIssue:
    """One actionable difference found during a snapshot roundtrip check."""

    domain: Literal["electrical", "placement"]
    path: str
    expected: Any
    actual: Any
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "path": self.path,
            "expected": _plain_value(self.expected),
            "actual": _plain_value(self.actual),
            "message": self.message,
        }


@dataclass(frozen=True)
class RoundtripReport:
    """Electrical and optional placement result of a roundtrip comparison."""

    ok: bool
    electrical_ok: bool
    placement_ok: bool
    issues: tuple[RoundtripIssue, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "electrical_ok": self.electrical_ok,
            "placement_ok": self.placement_ok,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _plain_value(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return {key: _plain_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_plain_value(item) for item in value)
    if isinstance(value, list):
        return [_plain_value(item) for item in value]
    return value


def _diagnostic_value(value: Any, *, limit: int = 160) -> str:
    rendered = repr(_plain_value(value))
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


def _strict_equal(expected: Any, actual: Any) -> bool:
    """Equality that does not let ``True`` equal ``1`` (or ``1.0``)."""

    if type(expected) is not type(actual):
        return False
    if isinstance(expected, Mapping) or isinstance(actual, Mapping):
        if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
            return False
        if set(expected) != set(actual):
            return False
        return all(_strict_equal(expected[key], actual[key]) for key in expected)
    if isinstance(expected, (tuple, list)) or isinstance(actual, (tuple, list)):
        if type(expected) is not type(actual) or len(expected) != len(actual):
            return False
        return all(_strict_equal(left, right) for left, right in zip(expected, actual))
    try:
        return expected == actual
    except (TypeError, ValueError):
        return False


def _numeric_or_strict_equal(
    expected: Any, actual: Any, *, numeric_param_equiv: bool, rel_tol: float
) -> bool:
    if _strict_equal(expected, actual):
        return True
    if not numeric_param_equiv or isinstance(expected, bool) or isinstance(actual, bool):
        return False
    expected_number = parse_engineering_number(expected)
    actual_number = parse_engineering_number(actual)
    if expected_number is None or actual_number is None:
        return False
    return math.isclose(expected_number, actual_number, rel_tol=rel_tol, abs_tol=0.0)


def _index_by_name(items: Iterable[Any]) -> dict[str, list[Any]]:
    indexed: dict[str, list[Any]] = {}
    for item in items:
        indexed.setdefault(item.name, []).append(item)
    return indexed


def _pin_occurrence_pairs(
    expected: Iterable[PinPlacement], actual: Iterable[PinPlacement]
) -> tuple[tuple[PinPlacement | None, PinPlacement | None], ...]:
    """Pair unchanged duplicate pins before pairing modified occurrences."""

    expected_values = sorted(expected, key=lambda item: repr(_plain_value(item)))
    actual_remaining = sorted(actual, key=lambda item: repr(_plain_value(item)))
    pairs: list[tuple[PinPlacement | None, PinPlacement | None]] = []
    for expected_item in expected_values:
        exact_index = next(
            (
                index
                for index, actual_item in enumerate(actual_remaining)
                if _strict_equal(expected_item, actual_item)
            ),
            None,
        )
        if exact_index is None:
            pairs.append((expected_item, None))
        else:
            pairs.append((expected_item, actual_remaining.pop(exact_index)))
    for index, (expected_item, actual_item) in enumerate(pairs):
        if actual_item is None and actual_remaining:
            pairs[index] = (expected_item, actual_remaining.pop(0))
    pairs.extend((None, actual_item) for actual_item in actual_remaining)
    return tuple(pairs)


def _append_collection_issues(
    issues: list[RoundtripIssue],
    *,
    domain: Literal["electrical", "placement"],
    collection: str,
    expected_items: Iterable[Any],
    actual_items: Iterable[Any],
) -> tuple[dict[str, list[Any]], dict[str, list[Any]]]:
    expected_by_name = _index_by_name(expected_items)
    actual_by_name = _index_by_name(actual_items)
    for name in sorted(set(expected_by_name) | set(actual_by_name)):
        expected_named = expected_by_name.get(name, [])
        actual_named = actual_by_name.get(name, [])
        path = f"{collection}[{name!r}]"
        if not expected_named:
            issues.append(
                RoundtripIssue(
                    domain,
                    path,
                    None,
                    actual_named,
                    f"Unexpected {collection[:-1]} {name!r}",
                )
            )
        elif not actual_named:
            issues.append(
                RoundtripIssue(
                    domain,
                    path,
                    expected_named,
                    None,
                    f"Missing {collection[:-1]} {name!r}",
                )
            )
        elif len(expected_named) != len(actual_named):
            issues.append(
                RoundtripIssue(
                    domain,
                    path,
                    expected_named,
                    actual_named,
                    f"Duplicate {collection[:-1]} entries differ in count",
                )
            )
    return expected_by_name, actual_by_name


def _compare_mapping(
    issues: list[RoundtripIssue],
    *,
    domain: Literal["electrical", "placement"],
    path: str,
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    numeric_values: bool = False,
    rel_tol: float = 1e-9,
) -> None:
    for key in sorted(set(expected) | set(actual), key=str):
        item_path = f"{path}[{key!r}]"
        if key not in actual:
            issues.append(
                RoundtripIssue(domain, item_path, expected[key], None, f"Missing value at {item_path}")
            )
        elif key not in expected:
            issues.append(
                RoundtripIssue(domain, item_path, None, actual[key], f"Unexpected value at {item_path}")
            )
        else:
            equal = (
                _numeric_or_strict_equal(
                    expected[key],
                    actual[key],
                    numeric_param_equiv=numeric_values,
                    rel_tol=rel_tol,
                )
                if numeric_values
                else _strict_equal(expected[key], actual[key])
            )
            if not equal:
                issues.append(
                    RoundtripIssue(
                        domain,
                        item_path,
                        expected[key],
                        actual[key],
                        f"Value at {item_path} differs",
                    )
                )


ANON_NET_RE = re.compile(r"^net[0-9]+$")


def _is_bus_net_name(net_name: str) -> bool:
    return "<" in net_name or ">" in net_name or "," in net_name or "*" in net_name


def explicit_net_names(snapshot: SchematicSnapshot) -> frozenset[str]:
    """Collect net names that must match literally (ports, pins, globals, wire labels, buses)."""

    names: set[str] = set()
    for port in snapshot.electrical.ports:
        if port.name:
            names.add(port.name)
    for pin in snapshot.placement.pins:
        if pin.name:
            names.add(pin.name)
        if pin.net:
            names.add(pin.net)
    for net in snapshot.electrical.nets:
        if (
            net.is_global
            or net.num_bits > 1
            or _is_bus_net_name(net.name)
            or net.name.endswith("!")
        ):
            names.add(net.name)
    for wire in snapshot.placement.wires:
        if wire.net and (_is_bus_net_name(wire.net) or wire.net.endswith("!")):
            names.add(wire.net)
        if wire.labels:
            if wire.net:
                names.add(wire.net)
            for label in wire.labels:
                label_text = label.text.strip()
                if label_text:
                    names.add(label_text)
    for instance in snapshot.electrical.instances:
        for net_name in instance.connections.values():
            if _is_bus_net_name(net_name) or net_name.endswith("!"):
                names.add(net_name)
    return frozenset(names)


def is_anonymous_net(
    net_name: str, explicit_nets: AbstractSet[str] = frozenset()
) -> bool:
    """Return True when ``net_name`` is a Virtuoso auto-generated anonymous net."""

    return (
        isinstance(net_name, str)
        and bool(ANON_NET_RE.fullmatch(net_name))
        and net_name not in explicit_nets
    )


def _flatten_connections(
    conns: Mapping[Any, Any],
) -> dict[tuple[str, str], str]:
    """Accept either {(inst, term): net} or {inst: {term: net}}."""

    flat: dict[tuple[str, str], str] = {}
    for key, value in conns.items():
        if isinstance(key, tuple) and len(key) == 2 and isinstance(value, str):
            flat[(str(key[0]), str(key[1]))] = value
        elif isinstance(key, str) and isinstance(value, Mapping):
            for term, net_name in value.items():
                flat[(key, str(term))] = str(net_name)
        else:
            raise ValueError(f"Unsupported connections entry: {key!r}: {value!r}")
    return flat


@dataclass(frozen=True)
class _IsomorphismMismatch:
    instance: str
    terminal: str
    expected_net: str | None
    actual_net: str | None
    message: str


def _check_anonymous_net_isomorphism_detailed(
    expected_conns: Mapping[Any, Any],
    actual_conns: Mapping[Any, Any],
    explicit_expected: AbstractSet[str] = frozenset(),
    explicit_actual: AbstractSet[str] = frozenset(),
) -> tuple[bool, dict[str, str], list[_IsomorphismMismatch]]:
    exp_map = _flatten_connections(expected_conns)
    act_map = _flatten_connections(actual_conns)
    mismatches: list[_IsomorphismMismatch] = []

    for inst, term in sorted(set(exp_map) - set(act_map)):
        e_net = exp_map[(inst, term)]
        mismatches.append(
            _IsomorphismMismatch(
                inst,
                term,
                e_net,
                None,
                f"Missing terminal connection {inst}.{term} (expected net {e_net!r})",
            )
        )
    for inst, term in sorted(set(act_map) - set(exp_map)):
        a_net = act_map[(inst, term)]
        mismatches.append(
            _IsomorphismMismatch(
                inst,
                term,
                None,
                a_net,
                f"Unexpected terminal connection {inst}.{term} (actual net {a_net!r})",
            )
        )

    forward: dict[str, str] = {}
    reverse: dict[str, str] = {}
    common_anon_exp_classes: dict[str, set[tuple[str, str]]] = {}
    common_anon_act_classes: dict[str, set[tuple[str, str]]] = {}

    for inst, term in sorted(set(exp_map) & set(act_map)):
        e_net = exp_map[(inst, term)]
        a_net = act_map[(inst, term)]
        e_anon = is_anonymous_net(e_net, explicit_expected)
        a_anon = is_anonymous_net(a_net, explicit_actual)

        if not e_anon and not a_anon:
            if e_net != a_net:
                mismatches.append(
                    _IsomorphismMismatch(
                        inst,
                        term,
                        e_net,
                        a_net,
                        f"{inst}.{term}: named net mismatch expected {e_net!r}, got {a_net!r}",
                    )
                )
        elif e_anon != a_anon:
            mismatches.append(
                _IsomorphismMismatch(
                    inst,
                    term,
                    e_net,
                    a_net,
                    f"{inst}.{term}: cannot map between anonymous net and named net "
                    f"(expected {e_net!r}, got {a_net!r})",
                )
            )
        else:
            common_anon_exp_classes.setdefault(e_net, set()).add((inst, term))
            common_anon_act_classes.setdefault(a_net, set()).add((inst, term))
            if e_net in forward:
                if forward[e_net] != a_net:
                    mismatches.append(
                        _IsomorphismMismatch(
                            inst,
                            term,
                            e_net,
                            a_net,
                            f"{inst}.{term}: anonymous net {e_net!r} split across "
                            f"{forward[e_net]!r} and {a_net!r}",
                        )
                    )
            elif a_net in reverse:
                mismatches.append(
                    _IsomorphismMismatch(
                        inst,
                        term,
                        e_net,
                        a_net,
                        f"{inst}.{term}: anonymous nets {reverse[a_net]!r} and {e_net!r} "
                        f"shorted into {a_net!r}",
                    )
                )
            else:
                forward[e_net] = a_net
                reverse[a_net] = e_net

    if not mismatches:
        # Final equivalence-class partition invariant check.
        for e_net, e_terms in sorted(common_anon_exp_classes.items()):
            a_net = forward.get(e_net)
            if a_net is None or common_anon_act_classes.get(a_net) != e_terms:
                first_inst, first_term = sorted(e_terms)[0]
                mismatches.append(
                    _IsomorphismMismatch(
                        first_inst,
                        first_term,
                        e_net,
                        a_net,
                        f"Anonymous net equivalence class mismatch for {e_net!r}",
                    )
                )

    if mismatches:
        return False, {}, mismatches
    return True, dict(sorted(forward.items())), []


def check_anonymous_net_isomorphism(
    expected_conns: Mapping[Any, Any],
    actual_conns: Mapping[Any, Any],
    explicit_expected: AbstractSet[str] = frozenset(),
    explicit_actual: AbstractSet[str] = frozenset(),
) -> tuple[bool, dict[str, str], list[str]]:
    """Verify connectivity up to a strict 1-to-1 bijection on anonymous auto-nets.

    Named, port, global, bus, and explicitly labeled nets must match literally.
    Returns ``(ok, bijection, mismatches)`` where ``bijection`` maps expected
    anonymous net names to actual anonymous net names when ``ok`` is True.
    """

    ok, bijection, detailed = _check_anonymous_net_isomorphism_detailed(
        expected_conns,
        actual_conns,
        explicit_expected,
        explicit_actual,
    )
    return ok, bijection, [item.message for item in detailed]


def _validate_coupling_rules_map(
    coupling_rules: Mapping[str, Sequence[CdfCouplingRule]] | None,
) -> dict[str, tuple[CdfCouplingRule, ...]]:
    if coupling_rules is None:
        return {}
    if not isinstance(coupling_rules, Mapping):
        raise SchemaValidationError("coupling_rules must be a mapping")
    validated: dict[str, tuple[CdfCouplingRule, ...]] = {}
    for master_key, raw_rules in sorted(coupling_rules.items()):
        if type(master_key) is not str or not master_key.strip():
            raise SchemaValidationError(f"invalid coupling_rules key: {master_key!r}")
        if isinstance(raw_rules, (str, bytes, bytearray)) or not isinstance(raw_rules, Sequence):
            raise SchemaValidationError(f"coupling_rules[{master_key!r}] must be a sequence")
        parsed: list[CdfCouplingRule] = []
        for idx, item in enumerate(raw_rules):
            if isinstance(item, CdfCouplingRule):
                parsed.append(item)
            elif isinstance(item, Mapping):
                parsed.append(CdfCouplingRule.from_dict(item))
            else:
                raise SchemaValidationError(
                    f"coupling_rules[{master_key!r}][{idx}] must be a CdfCouplingRule or mapping"
                )
        rules_tuple = tuple(parsed)
        resolve_cdf_callback_order((), coupling_rules=rules_tuple)
        validated[master_key] = rules_tuple
    return validated


def verify_derived_freshness(
    snapshot: SchematicSnapshot,
    *,
    coupling_rules: Mapping[str, Sequence[CdfCouplingRule]] | None = None,
) -> dict[str, Any]:
    """Check valid cdf-readback derived states against current primary inputs.

    Stale and unavailable states are reported cleanly without raising.
    Cyclic or malformed coupling rules fail closed with CdfParameterError / SchemaValidationError.
    """

    rules_map = _validate_coupling_rules_map(coupling_rules)
    fresh: list[str] = []
    stale: list[str] = []
    unavailable: list[str] = []
    unverified: list[str] = []
    mismatched: list[dict[str, Any]] = []
    instances_report: dict[str, dict[str, Any]] = {}

    for instance in snapshot.electrical.instances:
        derived = instance.effective_derived
        master_key = f"{instance.lib}/{instance.cell}"
        rules = tuple(rules_map.get(master_key, ()))
        expected_sha = compute_cdf_input_sha256(
            instance.lib,
            instance.cell,
            instance.view,
            instance.params,
            instance.param_types,
            coupling_rules=rules,
        )
        entry: dict[str, Any] = {
            "status": derived.status,
            "source": derived.source,
            "expected_input_sha256": expected_sha,
            "actual_input_sha256": derived.input_sha256,
        }
        if derived.status == "stale":
            stale.append(instance.name)
            entry["freshness"] = "stale"
        elif derived.status == "unavailable":
            unavailable.append(instance.name)
            entry["freshness"] = "unavailable"
        elif derived.source == "cdf-readback" and derived.input_sha256 is not None:
            if derived.input_sha256 == expected_sha:
                fresh.append(instance.name)
                entry["freshness"] = "fresh"
            else:
                entry["freshness"] = "mismatched"
                mismatched.append(
                    {
                        "instance": instance.name,
                        "expected_input_sha256": expected_sha,
                        "actual_input_sha256": derived.input_sha256,
                        "status": derived.status,
                        "source": derived.source,
                    }
                )
        else:
            unverified.append(instance.name)
            entry["freshness"] = "unverified"
        instances_report[instance.name] = entry

    return {
        "ok": len(mismatched) == 0,
        "fresh": fresh,
        "stale": stale,
        "unavailable": unavailable,
        "unverified": unverified,
        "mismatched": mismatched,
        "instances": instances_report,
    }


def _compare_electrical(
    expected: SchematicSnapshot,
    actual: SchematicSnapshot,
    *,
    numeric_param_equiv: bool,
    rel_tol: float,
) -> tuple[list[RoundtripIssue], dict[str, str]]:
    issues: list[RoundtripIssue] = []
    expected_electrical = expected.electrical
    actual_electrical = actual.electrical

    expected_ports, actual_ports = _append_collection_issues(
        issues,
        domain="electrical",
        collection="ports",
        expected_items=expected_electrical.ports,
        actual_items=actual_electrical.ports,
    )
    for name in sorted(set(expected_ports) & set(actual_ports)):
        if len(expected_ports[name]) != 1 or len(actual_ports[name]) != 1:
            continue
        expected_port = expected_ports[name][0]
        actual_port = actual_ports[name][0]
        if expected_port.direction != actual_port.direction:
            issues.append(
                RoundtripIssue(
                    "electrical",
                    f"ports[{name!r}].direction",
                    expected_port.direction,
                    actual_port.direction,
                    f"Port {name!r} direction differs",
                )
            )

    exp_explicit = explicit_net_names(expected)
    act_explicit = explicit_net_names(actual)
    exp_conns = {inst.name: inst.connections for inst in expected_electrical.instances}
    act_conns = {inst.name: inst.connections for inst in actual_electrical.instances}
    iso_ok, anon_bijection, iso_mismatches = _check_anonymous_net_isomorphism_detailed(
        exp_conns, act_conns, exp_explicit, act_explicit
    )
    if not iso_ok:
        for mismatch in iso_mismatches:
            issues.append(
                RoundtripIssue(
                    "electrical",
                    f"instances[{mismatch.instance!r}].connections[{mismatch.terminal!r}]",
                    mismatch.expected_net,
                    mismatch.actual_net,
                    mismatch.message,
                )
            )

    expected_nets_by_name = _index_by_name(expected_electrical.nets)
    actual_nets_by_name = _index_by_name(actual_electrical.nets)
    mapped_expected_nets = set(anon_bijection.keys())
    mapped_actual_nets = set(anon_bijection.values())

    for e_name, a_name in sorted(anon_bijection.items()):
        e_named = expected_nets_by_name.get(e_name, [])
        a_named = actual_nets_by_name.get(a_name, [])
        path = f"nets[{e_name!r}]"
        if not e_named and a_named:
            issues.append(
                RoundtripIssue(
                    "electrical",
                    f"nets[{a_name!r}]",
                    None,
                    a_named,
                    f"Unexpected net {a_name!r}",
                )
            )
            continue
        if e_named and not a_named:
            issues.append(
                RoundtripIssue(
                    "electrical",
                    path,
                    e_named,
                    None,
                    f"Missing net {e_name!r} (mapped to {a_name!r})",
                )
            )
            continue
        if len(e_named) != len(a_named):
            issues.append(
                RoundtripIssue(
                    "electrical",
                    path,
                    e_named,
                    a_named,
                    "Duplicate net entries differ in count",
                )
            )
            continue
        if len(e_named) == 1:
            expected_net = e_named[0]
            actual_net = a_named[0]
            if not _strict_equal(expected_net.is_global, actual_net.is_global):
                issues.append(
                    RoundtripIssue(
                        "electrical",
                        f"nets[{e_name!r}].is_global",
                        expected_net.is_global,
                        actual_net.is_global,
                        f"Net {e_name!r} global flag differs",
                    )
                )
            if not _strict_equal(expected_net.num_bits, actual_net.num_bits):
                issues.append(
                    RoundtripIssue(
                        "electrical",
                        f"nets[{e_name!r}].num_bits",
                        expected_net.num_bits,
                        actual_net.num_bits,
                        f"Net {e_name!r} width differs",
                    )
                )

    unmapped_expected_nets = [
        net for net in expected_electrical.nets if net.name not in mapped_expected_nets
    ]
    unmapped_actual_nets = [
        net for net in actual_electrical.nets if net.name not in mapped_actual_nets
    ]
    expected_nets, actual_nets = _append_collection_issues(
        issues,
        domain="electrical",
        collection="nets",
        expected_items=unmapped_expected_nets,
        actual_items=unmapped_actual_nets,
    )
    for name in sorted(set(expected_nets) & set(actual_nets)):
        if len(expected_nets[name]) != 1 or len(actual_nets[name]) != 1:
            continue
        expected_net = expected_nets[name][0]
        actual_net = actual_nets[name][0]
        if not _strict_equal(expected_net.is_global, actual_net.is_global):
            issues.append(
                RoundtripIssue(
                    "electrical",
                    f"nets[{name!r}].is_global",
                    expected_net.is_global,
                    actual_net.is_global,
                    f"Net {name!r} global flag differs",
                )
            )
        if not _strict_equal(expected_net.num_bits, actual_net.num_bits):
            issues.append(
                RoundtripIssue(
                    "electrical",
                    f"nets[{name!r}].num_bits",
                    expected_net.num_bits,
                    actual_net.num_bits,
                    f"Net {name!r} width differs",
                )
            )

    expected_instances, actual_instances = _append_collection_issues(
        issues,
        domain="electrical",
        collection="instances",
        expected_items=expected_electrical.instances,
        actual_items=actual_electrical.instances,
    )

    for name in sorted(set(expected_instances) & set(actual_instances)):
        if len(expected_instances[name]) != 1 or len(actual_instances[name]) != 1:
            continue
        expected_instance: Instance = expected_instances[name][0]
        actual_instance: Instance = actual_instances[name][0]
        for field_name in ("lib", "cell", "view"):
            expected_value = getattr(expected_instance, field_name)
            actual_value = getattr(actual_instance, field_name)
            if not _strict_equal(expected_value, actual_value):
                issues.append(
                    RoundtripIssue(
                        "electrical",
                        f"instances[{name!r}].{field_name}",
                        expected_value,
                        actual_value,
                        f"Instance {name!r} master {field_name} differs",
                    )
                )
        _compare_mapping(
            issues,
            domain="electrical",
            path=f"instances[{name!r}].params",
            expected=expected_instance.params,
            actual=actual_instance.params,
            numeric_values=numeric_param_equiv,
            rel_tol=rel_tol,
        )
        _compare_mapping(
            issues,
            domain="electrical",
            path=f"instances[{name!r}].param_types",
            expected=expected_instance.param_types,
            actual=actual_instance.param_types,
        )
        _compare_mapping(
            issues,
            domain="electrical",
            path=f"instances[{name!r}].props",
            expected=expected_instance.props,
            actual=actual_instance.props,
        )
        # derived / derived_params are read-only callback observations and are
        # intentionally excluded from primary electrical roundtrip equality.

    _compare_mapping(
        issues,
        domain="electrical",
        path="properties",
        expected=expected_electrical.properties,
        actual=actual_electrical.properties,
    )
    if not _strict_equal(expected_electrical.constraints, actual_electrical.constraints):
        issues.append(
            RoundtripIssue(
                "electrical",
                "constraints",
                expected_electrical.constraints,
                actual_electrical.constraints,
                "Schematic constraints differ",
            )
        )
    return issues, anon_bijection


def _coordinate_close(expected: float, actual: float, tolerance: float) -> bool:
    try:
        return math.isclose(float(expected), float(actual), rel_tol=0.0, abs_tol=tolerance)
    except (TypeError, ValueError, OverflowError):
        return False


def _xy_close(expected: tuple[float, float], actual: tuple[float, float], tolerance: float) -> bool:
    return len(expected) == 2 and len(actual) == 2 and all(
        _coordinate_close(left, right, tolerance) for left, right in zip(expected, actual)
    )


def _compare_instance_placements(
    issues: list[RoundtripIssue], expected: SchematicSnapshot, actual: SchematicSnapshot, tolerance: float
) -> None:
    expected_by_name, actual_by_name = _append_collection_issues(
        issues,
        domain="placement",
        collection="instance placements",
        expected_items=expected.placement.instances,
        actual_items=actual.placement.instances,
    )
    for name in sorted(set(expected_by_name) & set(actual_by_name)):
        if len(expected_by_name[name]) != 1 or len(actual_by_name[name]) != 1:
            continue
        expected_item: InstancePlacement = expected_by_name[name][0]
        actual_item: InstancePlacement = actual_by_name[name][0]
        if not _xy_close(expected_item.xy, actual_item.xy, tolerance):
            issues.append(
                RoundtripIssue(
                    "placement",
                    f"instance_placements[{name!r}].xy",
                    expected_item.xy,
                    actual_item.xy,
                    f"Instance placement {name!r} coordinates differ",
                )
            )
        if expected_item.orient != actual_item.orient:
            issues.append(
                RoundtripIssue(
                    "placement",
                    f"instance_placements[{name!r}].orient",
                    expected_item.orient,
                    actual_item.orient,
                    f"Instance placement {name!r} orientation differs",
                )
            )


def _compare_pin_placements(
    issues: list[RoundtripIssue], expected: SchematicSnapshot, actual: SchematicSnapshot, tolerance: float
) -> None:
    expected_by_name = _index_by_name(expected.placement.pins)
    actual_by_name = _index_by_name(actual.placement.pins)
    for name in sorted(set(expected_by_name) | set(actual_by_name)):
        expected_named = expected_by_name.get(name, [])
        actual_named = actual_by_name.get(name, [])
        if len(expected_named) != len(actual_named):
            issues.append(
                RoundtripIssue(
                    "placement",
                    f"pins[{name!r}]",
                    expected_named,
                    actual_named,
                    "Duplicate pin entries differ in count",
                )
            )
        pairs = _pin_occurrence_pairs(expected_named, actual_named)
        multiple = len(pairs) > 1
        for index, (expected_item, actual_item) in enumerate(pairs):
            path_prefix = f"pins[{name!r}]"
            if multiple:
                path_prefix += f"[{index}]"
            if expected_item is None:
                issues.append(
                    RoundtripIssue(
                        "placement",
                        path_prefix,
                        None,
                        actual_item,
                        f"Unexpected pin {name!r}",
                    )
                )
                continue
            if actual_item is None:
                issues.append(
                    RoundtripIssue(
                        "placement",
                        path_prefix,
                        expected_item,
                        None,
                        f"Missing pin {name!r}",
                    )
                )
                continue
            for field_name in ("net", "direction", "lib", "cell", "view", "orient"):
                expected_value = getattr(expected_item, field_name)
                actual_value = getattr(actual_item, field_name)
                if not _strict_equal(expected_value, actual_value):
                    issues.append(
                        RoundtripIssue(
                            "placement",
                            f"{path_prefix}.{field_name}",
                            expected_value,
                            actual_value,
                            f"Pin {name!r} {field_name} differs",
                        )
                    )
            if not _xy_close(expected_item.xy, actual_item.xy, tolerance):
                issues.append(
                    RoundtripIssue(
                        "placement",
                        f"{path_prefix}.xy",
                        expected_item.xy,
                        actual_item.xy,
                        f"Pin {name!r} coordinates differ",
                    )
                )


def _normalise_wire_points(points: tuple[tuple[float, float], ...]) -> tuple[tuple[float, float], ...]:
    forward = tuple(points)
    reverse = tuple(reversed(points))
    return min(forward, reverse) if len(forward) > 1 else forward


def _wire_geometry_close(expected: WireSegment, actual: WireSegment, tolerance: float) -> bool:
    expected_points = _normalise_wire_points(expected.points)
    actual_points = _normalise_wire_points(actual.points)
    return len(expected_points) == len(actual_points) and all(
        _xy_close(left, right, tolerance) for left, right in zip(expected_points, actual_points)
    )


def _wire_label_match(expected: WireLabel, actual: WireLabel, tolerance: float) -> bool:
    return (
        expected.text == actual.text
        and _xy_close(expected.xy, actual.xy, tolerance)
        and expected.justify == actual.justify
        and expected.orient == actual.orient
        and expected.font == actual.font
        and expected.height == actual.height
    )


def _wire_labels_match(
    expected: tuple[WireLabel, ...], actual: tuple[WireLabel, ...], tolerance: float
) -> bool:
    if len(expected) != len(actual):
        return False
    unmatched = list(actual)
    for expected_label in expected:
        for index, actual_label in enumerate(unmatched):
            if _wire_label_match(expected_label, actual_label, tolerance):
                unmatched.pop(index)
                break
        else:
            return False
    return True


def _wire_discrete_match(
    expected: WireSegment,
    actual: WireSegment,
    anon_bijection: Mapping[str, str],
    reverse_bijection: Mapping[str, str],
) -> bool:
    if expected.route_method != actual.route_method:
        return False
    if expected.net is not None and expected.net in anon_bijection:
        return actual.net == anon_bijection[expected.net]
    if actual.net is not None and actual.net in reverse_bijection:
        return False
    return expected.net == actual.net


def _quantize_wire_coord(value: float) -> float:
    rounded = round(float(value), 6)
    return 0.0 if rounded == 0.0 else rounded


def _quantize_wire_point(point: tuple[float, float]) -> tuple[float, float]:
    return (_quantize_wire_coord(point[0]), _quantize_wire_coord(point[1]))


def _canonical_wire_net(
    net: str | None,
    anon_bijection: Mapping[str, str],
    reverse_bijection: Mapping[str, str],
    *,
    is_expected: bool,
) -> tuple[str, int, str]:
    if is_expected:
        if net is not None and net in anon_bijection:
            return ("anon", 1, anon_bijection[net])
    else:
        if net is not None and net in reverse_bijection:
            return ("anon", 1, net)
    if net is None:
        return ("lit", 0, "")
    return ("lit", 1, net)


def _is_collinear_continuation(
    u: tuple[float, float], v: tuple[float, float], w: tuple[float, float]
) -> bool:
    dx1 = v[0] - u[0]
    dy1 = v[1] - u[1]
    dx2 = w[0] - v[0]
    dy2 = w[1] - v[1]
    len1 = math.hypot(dx1, dy1)
    len2 = math.hypot(dx2, dy2)
    if len1 <= 0.0 or len2 <= 0.0:
        return False
    cross = dx1 * dy2 - dy1 * dx2
    if abs(cross) > 1e-9 * len1 * len2:
        return False
    dot = dx1 * dx2 + dy1 * dy2
    return dot > 0.0


def _points_match_with_tol(
    expected_points: tuple[tuple[float, float], ...],
    actual_points: tuple[tuple[float, float], ...],
    tolerance: float,
) -> bool:
    if len(expected_points) != len(actual_points):
        return False
    if all(
        _xy_close(left, right, tolerance)
        for left, right in zip(expected_points, actual_points)
    ):
        return True
    return all(
        _xy_close(left, right, tolerance)
        for left, right in zip(expected_points, reversed(actual_points))
    )


def _coalesce_wire_set(
    wires: Sequence[WireSegment],
    anon_bijection: Mapping[str, str],
    reverse_bijection: Mapping[str, str],
    *,
    is_expected: bool,
) -> tuple[
    dict[tuple[tuple[str, int, str], float, str], list[tuple[tuple[float, float], ...]]],
    dict[tuple[str, int, str], list[WireLabel]],
    int,
]:
    """Coalesce touching collinear 2-point wire segments at degree-2 junctions only."""

    WireKey = tuple[tuple[str, int, str], float, str]
    segments: dict[int, tuple[WireKey, tuple[float, float], tuple[float, float]]] = {}
    vertex_incident: dict[tuple[float, float], set[int]] = {}
    blocked_vertices: set[tuple[float, float]] = set()
    fixed_geoms: dict[WireKey, list[tuple[tuple[float, float], ...]]] = {}
    labels_by_net: dict[tuple[str, int, str], list[WireLabel]] = {}
    next_seg_id = 0

    for wire in wires:
        canon_net = _canonical_wire_net(
            wire.net, anon_bijection, reverse_bijection, is_expected=is_expected
        )
        key: WireKey = (canon_net, _quantize_wire_coord(wire.width), wire.route_method)
        if wire.labels:
            labels_by_net.setdefault(canon_net, []).extend(wire.labels)
        q_pts = tuple(_quantize_wire_point(pt) for pt in wire.points)
        if len(q_pts) == 2 and q_pts[0] != q_pts[1]:
            p0, p1 = (q_pts[0], q_pts[1]) if q_pts[0] <= q_pts[1] else (q_pts[1], q_pts[0])
            seg_id = next_seg_id
            next_seg_id += 1
            segments[seg_id] = (key, p0, p1)
            vertex_incident.setdefault(p0, set()).add(seg_id)
            vertex_incident.setdefault(p1, set()).add(seg_id)
        else:
            for pt in q_pts:
                blocked_vertices.add(pt)
            fixed_geoms.setdefault(key, []).append(_normalise_wire_points(q_pts))

    merges = 0
    changed = True
    while changed:
        changed = False
        for vertex in sorted(vertex_incident):
            if vertex in blocked_vertices:
                continue
            inc = vertex_incident.get(vertex)
            if not inc or len(inc) != 2:
                continue
            seg_id_1, seg_id_2 = sorted(inc)
            key1, a1, b1 = segments[seg_id_1]
            key2, a2, b2 = segments[seg_id_2]
            if key1 != key2:
                continue
            u = b1 if a1 == vertex else a1
            w = b2 if a2 == vertex else a2
            if u == w or not _is_collinear_continuation(u, vertex, w):
                continue
            del segments[seg_id_1]
            del segments[seg_id_2]
            vertex_incident.pop(vertex, None)
            vertex_incident[u].discard(seg_id_1)
            vertex_incident[w].discard(seg_id_2)
            new_id = next_seg_id
            next_seg_id += 1
            p0, p1 = (u, w) if u <= w else (w, u)
            segments[new_id] = (key1, p0, p1)
            vertex_incident[p0].add(new_id)
            vertex_incident[p1].add(new_id)
            merges += 1
            changed = True

    grouped: dict[WireKey, list[tuple[tuple[float, float], ...]]] = {}
    for key, geoms in fixed_geoms.items():
        grouped.setdefault(key, []).extend(geoms)
    for key, p0, p1 in segments.values():
        grouped.setdefault(key, []).append((p0, p1) if p0 <= p1 else (p1, p0))
    for key in grouped:
        grouped[key].sort()
    return grouped, labels_by_net, merges


def _wires_coalesced_equivalent(
    expected_wires: Sequence[WireSegment],
    actual_wires: Sequence[WireSegment],
    tolerance: float,
    anon_bijection: Mapping[str, str],
    reverse_bijection: Mapping[str, str],
) -> bool:
    exp_geoms, exp_labels, exp_merges = _coalesce_wire_set(
        expected_wires, anon_bijection, reverse_bijection, is_expected=True
    )
    act_geoms, act_labels, act_merges = _coalesce_wire_set(
        actual_wires, anon_bijection, reverse_bijection, is_expected=False
    )
    if exp_merges == 0 and act_merges == 0:
        return False
    if set(exp_geoms) != set(act_geoms):
        return False
    for key in sorted(exp_geoms):
        e_list = exp_geoms[key]
        a_list = list(act_geoms[key])
        if len(e_list) != len(a_list):
            return False
        if e_list == a_list:
            continue
        for e_pts in e_list:
            for idx, a_pts in enumerate(a_list):
                if _points_match_with_tol(e_pts, a_pts, tolerance):
                    a_list.pop(idx)
                    break
            else:
                return False
    if set(exp_labels) != set(act_labels):
        return False
    for canon_net in sorted(exp_labels):
        if not _wire_labels_match(
            tuple(exp_labels[canon_net]),
            tuple(act_labels[canon_net]),
            tolerance,
        ):
            return False
    return True


def _compare_wires(
    issues: list[RoundtripIssue],
    expected: SchematicSnapshot,
    actual: SchematicSnapshot,
    tolerance: float,
    anon_bijection: Mapping[str, str] = {},
) -> None:
    expected_wires = expected.placement.wires
    actual_wires = actual.placement.wires
    if expected_wires == actual_wires and all(
        left == right for left, right in anon_bijection.items()
    ):
        return
    reverse_bijection = {v: k for k, v in anon_bijection.items()}
    direct_issues: list[RoundtripIssue] = []
    used_actual: set[int] = set()
    for expected_index, expected_wire in enumerate(expected_wires):
        geometry_match: int | None = None
        discrete_match: int | None = None
        geometry_candidates: list[int] = []
        for actual_index, actual_wire in enumerate(actual_wires):
            if actual_index in used_actual or not _wire_discrete_match(
                expected_wire, actual_wire, anon_bijection, reverse_bijection
            ):
                continue
            if discrete_match is None:
                discrete_match = actual_index
            if _wire_geometry_close(expected_wire, actual_wire, tolerance):
                geometry_candidates.append(actual_index)
        if geometry_candidates:
            geometry_match = next(
                (
                    actual_index
                    for actual_index in geometry_candidates
                    if actual_wires[actual_index].width == expected_wire.width
                    and _wire_labels_match(
                        expected_wire.labels,
                        actual_wires[actual_index].labels,
                        tolerance,
                    )
                ),
                geometry_candidates[0],
            )
        matched_index = geometry_match if geometry_match is not None else discrete_match
        path = f"wires[{expected_index}]"
        if matched_index is None:
            direct_issues.append(
                RoundtripIssue(
                    "placement",
                    path,
                    expected_wire,
                    None,
                    "Missing wire or wire electrical routing differs",
                )
            )
            continue

        used_actual.add(matched_index)
        actual_wire = actual_wires[matched_index]
        if geometry_match is None:
            direct_issues.append(
                RoundtripIssue(
                    "placement",
                    f"{path}.points",
                    expected_wire.points,
                    actual_wire.points,
                    "Wire endpoints or intermediate points differ",
                )
            )
        if expected_wire.width != actual_wire.width:
            direct_issues.append(
                RoundtripIssue(
                    "placement",
                    f"{path}.width",
                    expected_wire.width,
                    actual_wire.width,
                    "Wire width differs",
                )
            )
        if not _wire_labels_match(expected_wire.labels, actual_wire.labels, tolerance):
            direct_issues.append(
                RoundtripIssue(
                    "placement",
                    f"{path}.labels",
                    expected_wire.labels,
                    actual_wire.labels,
                    "Wire labels differ",
                )
            )

    for actual_index, actual_wire in enumerate(actual_wires):
        if actual_index not in used_actual:
            direct_issues.append(
                RoundtripIssue(
                    "placement",
                    f"wires[{actual_index}]",
                    None,
                    actual_wire,
                    "Unexpected wire",
                )
            )

    if not direct_issues:
        return
    if _wires_coalesced_equivalent(
        expected_wires, actual_wires, tolerance, anon_bijection, reverse_bijection
    ):
        return
    issues.extend(direct_issues)


def _note_label_match(expected: NoteLabel, actual: NoteLabel, tolerance: float) -> bool:
    return (
        expected.text == actual.text
        and _xy_close(expected.xy, actual.xy, tolerance)
        and expected.layer == actual.layer
        and expected.purpose == actual.purpose
        and expected.justify == actual.justify
        and expected.orient == actual.orient
        and expected.font == actual.font
        and expected.height == actual.height
    )


def _compare_note_labels(
    issues: list[RoundtripIssue], expected: SchematicSnapshot, actual: SchematicSnapshot, tolerance: float
) -> None:
    unmatched = list(actual.placement.labels)
    for index, expected_label in enumerate(expected.placement.labels):
        for actual_index, actual_label in enumerate(unmatched):
            if _note_label_match(expected_label, actual_label, tolerance):
                unmatched.pop(actual_index)
                break
        else:
            issues.append(
                RoundtripIssue(
                    "placement",
                    f"labels[{index}]",
                    expected_label,
                    None,
                    "Missing note label",
                )
            )
    for index, actual_label in enumerate(unmatched):
        issues.append(
            RoundtripIssue(
                "placement",
                f"labels[extra:{index}]",
                None,
                actual_label,
                "Unexpected note label",
            )
        )


def _normalize_rect_points(
    points: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    if len(points) != 2:
        return points
    (x0, y0), (x1, y1) = points
    return ((min(x0, x1), min(y0, y1)), (max(x0, x1), max(y0, y1)))


def _annotation_shape_match(
    expected: AnnotationShape, actual: AnnotationShape, tolerance: float
) -> bool:
    if (
        expected.kind != actual.kind
        or expected.layer != actual.layer
        or expected.purpose != actual.purpose
        or not _coordinate_close(expected.width, actual.width, tolerance)
        or len(expected.points) != len(actual.points)
    ):
        return False
    if expected.kind == "rect":
        exp_pts = _normalize_rect_points(expected.points)
        act_pts = _normalize_rect_points(actual.points)
        return all(_xy_close(l, r, tolerance) for l, r in zip(exp_pts, act_pts))
    if expected.kind == "line":
        forward = all(
            _xy_close(l, r, tolerance) for l, r in zip(expected.points, actual.points)
        )
        if forward:
            return True
        return all(
            _xy_close(l, r, tolerance)
            for l, r in zip(expected.points, reversed(actual.points))
        )
    return all(_xy_close(l, r, tolerance) for l, r in zip(expected.points, actual.points))


def _compare_annotations(
    issues: list[RoundtripIssue],
    expected: SchematicSnapshot,
    actual: SchematicSnapshot,
    tolerance: float,
) -> None:
    unmatched = list(actual.placement.annotations)
    for index, expected_shape in enumerate(expected.placement.annotations):
        for actual_index, actual_shape in enumerate(unmatched):
            if _annotation_shape_match(expected_shape, actual_shape, tolerance):
                unmatched.pop(actual_index)
                break
        else:
            issues.append(
                RoundtripIssue(
                    "placement",
                    f"annotations[{index}]",
                    expected_shape,
                    None,
                    "Missing annotation shape",
                )
            )
    for index, actual_shape in enumerate(unmatched):
        issues.append(
            RoundtripIssue(
                "placement",
                f"annotations[extra:{index}]",
                None,
                actual_shape,
                "Unexpected annotation shape",
            )
        )


def _compare_placement(
    expected: SchematicSnapshot,
    actual: SchematicSnapshot,
    tolerance: float,
    *,
    anon_bijection: Mapping[str, str] = {},
) -> list[RoundtripIssue]:
    issues: list[RoundtripIssue] = []
    _compare_instance_placements(issues, expected, actual, tolerance)
    _compare_pin_placements(issues, expected, actual, tolerance)
    _compare_wires(issues, expected, actual, tolerance, anon_bijection)
    _compare_note_labels(issues, expected, actual, tolerance)
    _compare_annotations(issues, expected, actual, tolerance)
    if not _strict_equal(expected.placement.sheet, actual.placement.sheet):
        issues.append(
            RoundtripIssue(
                "placement",
                "sheet",
                expected.placement.sheet,
                actual.placement.sheet,
                "Schematic sheet metadata differs",
            )
        )
    return issues


def schematic_port_order(snapshot: SchematicSnapshot) -> tuple[str, ...] | None:
    """Return the captured terminal order from manifest metadata, if present.

    ``export_schematic.il`` records the native ``cv~>terminals`` order as
    ``metadata.port_order``.  The canonical electrical model sorts ports by
    name, so restore must carry this order explicitly to recreate terminals in
    the order they had in Virtuoso.  A missing entry (older snapshots) returns
    ``None`` and leaves the historical name-sorted behavior in place; an entry
    that is not a permutation of the snapshot terminals is rejected.
    """

    if not isinstance(snapshot, SchematicSnapshot):
        raise SchemaValidationError("snapshot must be a SchematicSnapshot")
    raw = snapshot.manifest.metadata.get("port_order")
    if raw is None:
        return None
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise SchemaValidationError("metadata.port_order must be a list of terminal names")
    names = tuple(raw)
    if any(not isinstance(name, str) or not name for name in names):
        raise SchemaValidationError("metadata.port_order must contain non-empty strings")
    if len(set(names)) != len(names):
        raise SchemaValidationError("metadata.port_order contains duplicate terminal names")
    expected = {port.name for port in snapshot.electrical.ports}
    expected.update(pin.name for pin in snapshot.placement.pins)
    if set(names) != expected:
        missing = sorted(expected - set(names))
        unknown = sorted(set(names) - expected)
        raise SchemaValidationError(
            "metadata.port_order must list every schematic terminal exactly once"
            f" (missing={missing}, unknown={unknown})"
        )
    return names


def _compare_port_order(
    expected: SchematicSnapshot,
    actual: SchematicSnapshot,
) -> list[RoundtripIssue]:
    """Compare the relative order of declared ports after a restore.

    Only ports are compared.  Pin figures are created in a separate pass by
    ``restore_schematic.il``, so the interleaving of pins and ports in
    ``cv~>terminals`` cannot always be reproduced; the port subsequence can.
    """

    expected_order = schematic_port_order(expected)
    if expected_order is None:
        return []
    actual_order = schematic_port_order(actual)
    port_names = {port.name for port in expected.electrical.ports}
    expected_ports = [name for name in expected_order if name in port_names]
    actual_ports = (
        [name for name in actual_order if name in port_names]
        if actual_order is not None
        else []
    )
    if expected_ports != actual_ports:
        return [
            RoundtripIssue(
                "electrical",
                "metadata.port_order",
                expected_ports,
                actual_ports,
                "Declared port order differs",
            )
        ]
    return []


def verify_roundtrip(
    expected: SchematicSnapshot,
    actual: SchematicSnapshot,
    *,
    check_placement: bool = True,
    coord_tol: float = 1e-4,
    numeric_param_equiv: bool = True,
    rel_tol: float = 1e-9,
    raise_on_mismatch: bool = True,
) -> RoundtripReport:
    """Compare restored readback against the source snapshot.

    Electrical truth is always checked.  Placement is checked when requested,
    with endpoint and coordinate comparisons using ``coord_tol``.  Anonymous
    auto-nets (``^net[0-9]+$`` excluding explicit ports, pins, globals, wire
    labels, and buses) are verified by strict 1-to-1 connectivity bijection.
    When manifest metadata carries ``port_order``, the declared-port
    subsequence is compared as well.  Manifest digests and derived/read-only
    parameters are intentionally excluded from the comparison because a backend
    may regenerate them during readback.
    """

    if coord_tol < 0 or not math.isfinite(coord_tol):
        raise ValueError("coord_tol must be a finite non-negative number")
    if rel_tol < 0 or not math.isfinite(rel_tol):
        raise ValueError("rel_tol must be a finite non-negative number")

    electrical_issues, anon_bijection = _compare_electrical(
        expected,
        actual,
        numeric_param_equiv=numeric_param_equiv,
        rel_tol=rel_tol,
    )
    placement_issues = (
        _compare_placement(expected, actual, coord_tol, anon_bijection=anon_bijection)
        if check_placement
        else []
    )
    port_order_issues = _compare_port_order(expected, actual)
    issues = tuple(electrical_issues + placement_issues + port_order_issues)
    report = RoundtripReport(
        ok=not issues,
        electrical_ok=not electrical_issues,
        placement_ok=not placement_issues,
        issues=issues,
    )
    if issues and raise_on_mismatch:
        details = "; ".join(
            f"{issue.domain}:{issue.path} expected={_diagnostic_value(issue.expected)} "
            f"actual={_diagnostic_value(issue.actual)} ({issue.message})"
            for issue in issues[:3]
        )
        if len(issues) > 3:
            details += f"; ... and {len(issues) - 3} more"
        raise RoundtripMismatchError(f"Roundtrip mismatch ({len(issues)} issue(s)): {details}")
    return report


__all__ = [
    "ANON_NET_RE",
    "LockReport",
    "probe_locks",
    "ensure_no_live_locks",
    "ensure_library_allowed",
    "ensure_no_omissions",
    "parse_engineering_number",
    "explicit_net_names",
    "is_anonymous_net",
    "check_anonymous_net_isomorphism",
    "verify_derived_freshness",
    "RoundtripIssue",
    "RoundtripReport",
    "schematic_port_order",
    "verify_roundtrip",
]
