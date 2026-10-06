"""Aggregate and print Monte Carlo trial diagnostics + conditional win rates."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from scripts.monte_carlo.config import SimStats

INVESTIGATIVE_ROLES: Set[str] = {"Sheriff", "Investigator", "Lookout", "Tracker", "Mole"}
DISRUPTOR_MAFIA: Set[str] = {"Consort", "Gatekeeper", "Hypnotist", "Framer"}


@dataclass
class CondBucket:
    n: int = 0
    town: int = 0
    mafia: int = 0
    draw: int = 0


@dataclass
class ConditionalStats:
    buckets: Dict[str, CondBucket] = field(default_factory=dict)

    def record(self, key: str, res: Dict[str, bool]) -> None:
        b = self.buckets.setdefault(key, CondBucket())
        b.n += 1
        if res.get("Town"):
            b.town += 1
        if res.get("Mafia"):
            b.mafia += 1
        if res.get("Draw"):
            b.draw += 1

    def record_if(self, key: str, res: Dict[str, bool], condition: bool) -> None:
        if condition:
            self.record(key, res)


def accumulate_conditionals(
    res: Dict[str, bool],
    st: SimStats,
    roles: List[str],
) -> ConditionalStats:
    cs = ConditionalStats()
    days = int(st.get("days", 0))
    lynches = int(st.get("lynches", 0))
    town_ml = int(st.get("mislynches", 0))
    legacy_ml = int(st.get("mislynches_incl_neutrals", 0))
    doc_saves = int(st.get("doc_saves", 0))
    roleblocks = int(st.get("roleblocks", 0))
    gk_blocks = int(st.get("gatekeeper_blocks", 0))
    controls = int(st.get("controls", 0))
    ignites = int(st.get("ignites", 0))
    night_deaths = int(st.get("night_deaths", 0))
    role_set = set(roles)

    # Mislynches
    cs.record_if("town_mislynch>=1", res, town_ml >= 1)
    cs.record_if("town_mislynch=0", res, town_ml == 0)
    cs.record_if("legacy_mislynch>=1", res, legacy_ml >= 1)
    cs.record_if("legacy_mislynch=0", res, legacy_ml == 0)
    cs.record_if("neutral_lynched>=1", res, int(st.get("lynches_neutral", 0)) >= 1)

    # Lynch activity
    cs.record_if("lynch>=1", res, lynches >= 1)
    cs.record_if("lynch=0", res, lynches == 0)

    # Night / protection
    cs.record_if("doc_save>=1", res, doc_saves >= 1)
    cs.record_if("doc_save=0", res, doc_saves == 0)
    cs.record_if("roleblock>=1", res, roleblocks >= 1)
    cs.record_if("roleblock=0", res, roleblocks == 0)
    cs.record_if("gk_block>=1", res, gk_blocks >= 1)
    cs.record_if("witch_control>=1", res, controls >= 1)
    cs.record_if("arso_ignite>=1", res, ignites >= 1)
    cs.record_if("night_deaths>=4", res, night_deaths >= 4)
    cs.record_if("night_deaths<=2", res, night_deaths <= 2)

    # Game length
    cs.record_if("days>=4", res, days >= 4)
    cs.record_if("days<=2", res, days <= 2)
    cs.record_if("days>=6", res, days >= 6)

    # Role presence in lobby
    for role in (
        "Doctor",
        "Sheriff",
        "Psychic",
        "Vigilante",
        "Investigator",
        "Lookout",
        "Bodyguard",
        "Mayor",
        "Consort",
        "Gatekeeper",
        "Mole",
        "Framer",
        "Hypnotist",
        "Survivor",
        "Jester",
        "Arsonist",
        "Witch",
    ):
        cs.record_if(f"role:{role}", res, role in role_set)

    inv_count = sum(1 for r in role_set if r in INVESTIGATIVE_ROLES)
    cs.record_if("investigative>=2", res, inv_count >= 2)
    cs.record_if("investigative=1", res, inv_count == 1)
    cs.record_if("investigative=0", res, inv_count == 0)
    cs.record_if("disruptor_mafia>=1", res, any(r in role_set for r in DISRUPTOR_MAFIA))

    return cs


def merge_conditional_stats(target: ConditionalStats, other: ConditionalStats) -> None:
    for key, ob in other.buckets.items():
        tb = target.buckets.setdefault(key, CondBucket())
        tb.n += ob.n
        tb.town += ob.town
        tb.mafia += ob.mafia
        tb.draw += ob.draw


def _line(b: CondBucket, label: str) -> Optional[str]:
    if b.n == 0:
        return None
    return (
        f"  P(Town | {label})={b.town / b.n:.3f}  P(Mafia | {label})={b.mafia / b.n:.3f}  "
        f"P(Draw | {label})={b.draw / b.n:.3f}  (n={b.n})"
    )


def print_conditional_report(cs: ConditionalStats) -> None:
    sections: List[tuple[str, List[str]]] = [
        (
            "Mislynches",
            [
                "town_mislynch>=1",
                "town_mislynch=0",
                "legacy_mislynch>=1",
                "legacy_mislynch=0",
                "neutral_lynched>=1",
            ],
        ),
        (
            "Lynch activity",
            ["lynch>=1", "lynch=0"],
        ),
        (
            "Night actions",
            [
                "doc_save>=1",
                "doc_save=0",
                "roleblock>=1",
                "roleblock=0",
                "gk_block>=1",
                "witch_control>=1",
                "arso_ignite>=1",
                "night_deaths>=4",
                "night_deaths<=2",
            ],
        ),
        (
            "Game length",
            ["days<=2", "days>=4", "days>=6"],
        ),
        (
            "Town roles in lobby",
            [
                "role:Doctor",
                "role:Sheriff",
                "role:Psychic",
                "role:Vigilante",
                "role:Investigator",
                "role:Lookout",
                "role:Bodyguard",
                "role:Mayor",
            ],
        ),
        (
            "Mafia support in lobby",
            ["role:Consort", "role:Gatekeeper", "role:Mole", "role:Framer", "role:Hypnotist", "disruptor_mafia>=1"],
        ),
        (
            "Investigative density",
            ["investigative=0", "investigative=1", "investigative>=2"],
        ),
        (
            "Neutrals in lobby",
            ["role:Survivor", "role:Jester", "role:Arsonist", "role:Witch"],
        ),
    ]
    print("Conditional faction WR (P(win | condition), correct denominator):")
    for title, keys in sections:
        lines = []
        for key in keys:
            b = cs.buckets.get(key)
            if b is None:
                continue
            ln = _line(b, key)
            if ln:
                lines.append(ln)
        if lines:
            print(f"  [{title}]")
            for ln in lines:
                print(ln)


def print_avg_diagnostics(diag_sum: SimStats, trials: int) -> None:
    print("Diagnostics (averages per game):")
    for k in [
        "days",
        "lynches",
        "mislynches",
        "mislynches_incl_neutrals",
        "lynches_neutral",
        "night_deaths",
        "doc_saves",
        "roleblocks",
        "gatekeeper_blocks",
        "controls",
        "ignites",
    ]:
        print(f"  {k}: {diag_sum.get(k, 0) / trials:.3f}")
