"""Composed night and tribunal state for :class:`game.Game`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set, Type


@dataclass
class NightState:
    """Persisted night-phase fields (flattened on ``Game`` via delegated properties)."""

    night_actions: Dict[int, Dict] = field(default_factory=dict)
    doused_players: Set[int] = field(default_factory=set)
    night_death_causes: Dict[int, str] = field(default_factory=dict)
    night_completion_snapshot: Optional[Dict[str, Any]] = None
    psychic_visions_delivered_this_night: bool = False

    def clear_persisted(self) -> None:
        """Reset persisted night fields. Used by ``Game.reset`` / ``nuke_reset``."""
        self.night_actions.clear()
        self.doused_players.clear()
        self.night_death_causes.clear()
        self.night_completion_snapshot = None
        self.psychic_visions_delivered_this_night = False


@dataclass
class TribunalState:
    """Persisted tribunal / day-vote fields (on ``Game.tribunal_state``; flat attrs via delegated properties)."""

    vote_in_progress: bool = False
    votes_today: int = 0
    tribunal_muted: bool = False
    tribunal_defendant_id: Optional[int] = None
    tribunal_defense_deadline_utc: Optional[str] = None
    tribunal_judgment_deadline_utc: Optional[str] = None
    tribunal_judgment_message_id: Optional[int] = None
    tribunal_subphase: Optional[str] = None
    tribunal_last_words_deadline_utc: Optional[str] = None
    tribunal_verdict_committed: bool = False
    tribunal_resolved_judgments: Dict[str, str] = field(default_factory=dict)
    tribunal_guilty_vote_count: int = 0
    tribunal_innocent_vote_count: int = 0
    tribunal_mayor_voted: bool = False
    tribunal_lynch_finisher_done: bool = False
    tribunal_last_words_open_posted: bool = False

    def clear_persisted(
        self,
        *,
        keep_verdict_committed: bool = False,
        keep_vote_in_progress: bool = False,
    ) -> None:
        """Reset persisted tribunal fields.

        Callers:
        - ``teardown_tribunal(full_clear)``: defaults (clears verdict); ``keep_vote_in_progress=True``.
        - ``teardown_tribunal(live_vote_finally)``: ``keep_vote_in_progress=True``; verdict cleared.
        - Innocent pardon / win-check: ``keep_verdict_committed=True``, ``keep_vote_in_progress=True``.
        - ``Game.reset`` / ``nuke_reset``: both flags default False (full wipe).
        ``votes_today`` is never cleared here — only game reset sets it to 0.
        """
        if not keep_vote_in_progress:
            self.vote_in_progress = False
        self.tribunal_muted = False
        self.tribunal_defendant_id = None
        self.tribunal_defense_deadline_utc = None
        self.tribunal_judgment_deadline_utc = None
        self.tribunal_judgment_message_id = None
        self.tribunal_subphase = None
        self.tribunal_last_words_deadline_utc = None
        self.tribunal_resolved_judgments.clear()
        self.tribunal_guilty_vote_count = 0
        self.tribunal_innocent_vote_count = 0
        self.tribunal_mayor_voted = False
        self.tribunal_lynch_finisher_done = False
        self.tribunal_last_words_open_posted = False
        if not keep_verdict_committed:
            self.tribunal_verdict_committed = False


NIGHT_DELEGATE_ATTRS: tuple[str, ...] = (
    "night_actions",
    "doused_players",
    "night_death_causes",
    "night_completion_snapshot",
    "psychic_visions_delivered_this_night",
)

TRIBUNAL_DELEGATE_ATTRS: tuple[str, ...] = (
    "vote_in_progress",
    "votes_today",
    "tribunal_muted",
    "tribunal_defendant_id",
    "tribunal_defense_deadline_utc",
    "tribunal_judgment_deadline_utc",
    "tribunal_judgment_message_id",
    "tribunal_subphase",
    "tribunal_last_words_deadline_utc",
    "tribunal_verdict_committed",
    "tribunal_resolved_judgments",
    "tribunal_guilty_vote_count",
    "tribunal_innocent_vote_count",
    "tribunal_mayor_voted",
    "tribunal_lynch_finisher_done",
    "tribunal_last_words_open_posted",
)


def _delegate_property(host_cls: Type[object], name: str, sub_attr: str) -> None:
    def getter(self, _n: str = name, _s: str = sub_attr):
        return getattr(getattr(self, _s), _n)

    def setter(self, value: object, _n: str = name, _s: str = sub_attr) -> None:
        setattr(getattr(self, _s), _n, value)

    setattr(host_cls, name, property(getter, setter))


def install_game_state_delegates(host_cls: Type[object]) -> None:
    """Expose ``NightState`` / ``TribunalState`` fields on ``Game`` as flat attributes."""
    for attr in NIGHT_DELEGATE_ATTRS:
        _delegate_property(host_cls, attr, "night")
    for attr in TRIBUNAL_DELEGATE_ATTRS:
        _delegate_property(host_cls, attr, "tribunal_state")
