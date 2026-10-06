"""Night outcome fingerprints and golden parity checks for sim harnesses."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple


@dataclass(frozen=True)
class NightOutcomeFingerprint:
    deaths: FrozenSet[int]
    causes: Tuple[Tuple[int, str], ...]
    blocked: FrozenSet[int]
    transport_swaps: Tuple[Tuple[int, int], ...]

    def key(self) -> str:
        payload = {
            "deaths": sorted(self.deaths),
            "causes": list(self.causes),
            "blocked": sorted(self.blocked),
            "transport_swaps": [list(p) for p in self.transport_swaps],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


@dataclass
class OutcomeCoverageReport:
    nights_run: int = 0
    distinct_fingerprints: int = 0
    thin_lineups: List[str] = field(default_factory=list)
    fingerprint_counts: Dict[str, int] = field(default_factory=dict)


def night_outcome_fingerprint(game: Any, out: Mapping[str, Any]) -> NightOutcomeFingerprint:
    deaths = frozenset(int(x) for x in (out.get("deaths") or set()))
    causes_raw = getattr(game, "night_death_causes", {}) or {}
    causes = tuple(sorted((int(k), str(v)) for k, v in causes_raw.items()))
    blocked = frozenset(int(x) for x in (out.get("blocked") or []))
    swaps_raw = list(out.get("night_transport_swaps") or getattr(game, "night_transport_swaps", []) or [])
    swaps: List[Tuple[int, int]] = []
    for item in swaps_raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            try:
                swaps.append((int(item[0]), int(item[1])))
            except (TypeError, ValueError):
                continue
    return NightOutcomeFingerprint(
        deaths=deaths,
        causes=causes,
        blocked=blocked,
        transport_swaps=tuple(sorted(swaps)),
    )


def assert_night_outcome_consistency(game: Any, out: Mapping[str, Any]) -> None:
    """Deaths reported by pipeline must match night_death_causes keys."""
    deaths = frozenset(int(x) for x in (out.get("deaths") or set()))
    causes = getattr(game, "night_death_causes", {}) or {}
    cause_ids = frozenset(int(k) for k in causes.keys())
    assert deaths == cause_ids, (
        f"deaths/causes mismatch: deaths={sorted(deaths)} causes={sorted(cause_ids)}"
    )


def assert_golden_outcome(
    game: Any,
    out: Mapping[str, Any],
    *,
    expected_deaths: Iterable[int],
    expected_causes: Optional[Mapping[int, str]] = None,
    expected_blocked: Optional[Iterable[int]] = None,
    label: str = "golden",
) -> None:
    assert_night_outcome_consistency(game, out)
    fp = night_outcome_fingerprint(game, out)
    exp_deaths = frozenset(int(x) for x in expected_deaths)
    assert fp.deaths == exp_deaths, f"{label}: deaths expected {sorted(exp_deaths)} got {sorted(fp.deaths)}"
    if expected_causes is not None:
        exp_causes = tuple(sorted((int(k), str(v)) for k, v in expected_causes.items()))
        assert fp.causes == exp_causes, f"{label}: causes expected {exp_causes} got {fp.causes}"
    if expected_blocked is not None:
        exp_blocked = frozenset(int(x) for x in expected_blocked)
        assert fp.blocked == exp_blocked, f"{label}: blocked expected {sorted(exp_blocked)} got {sorted(fp.blocked)}"


class OutcomeCoverageTracker:
    """Track distinct outcome fingerprints per lineup key (roles tuple)."""

    def __init__(self, *, thin_threshold: int = 2) -> None:
        self.thin_threshold = int(thin_threshold)
        self._by_lineup: Dict[str, Set[str]] = {}
        self._global_counts: Dict[str, int] = {}
        self.nights_run = 0

    def record(self, lineup_key: str, fp: NightOutcomeFingerprint) -> None:
        self.nights_run += 1
        k = fp.key()
        self._by_lineup.setdefault(lineup_key, set()).add(k)
        self._global_counts[k] = self._global_counts.get(k, 0) + 1

    def finish(self) -> OutcomeCoverageReport:
        thin = [lk for lk, fps in self._by_lineup.items() if len(fps) < self.thin_threshold]
        return OutcomeCoverageReport(
            nights_run=self.nights_run,
            distinct_fingerprints=len(self._global_counts),
            thin_lineups=sorted(thin),
            fingerprint_counts=dict(sorted(self._global_counts.items(), key=lambda kv: -kv[1])),
        )
