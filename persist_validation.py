"""Normalize JSON checkpoint payloads into the shapes consumed by the night engine."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from night_resume import normalize_night_completion_snapshot


def _integer(value: object, *, minimum: int = 1) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        result = int(value)
    except ValueError:
        return None
    return result if result >= minimum else None


def _ids(value: object) -> list[int]:
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(pid for item in value if (pid := _integer(item)) is not None))


def _flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return isinstance(value, str) and value.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def normalize_night_transport_swaps(raw: object) -> list[tuple[int, int, int]]:
    """Restore ordered (target A, target B, transporter) records without truncation."""
    if not isinstance(raw, (list, tuple)):
        return []
    result = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            continue
        values = tuple(_integer(value) for value in item)
        if None not in values and values[0] != values[1]:
            result.append(values)
    return result


def normalize_night_completion_snapshot_for_game(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    out = deepcopy(raw)
    # The existing parser defines the checkpoint flags. Normalize their raw values
    # first: a corrupt nonempty string must not mark a phase as already completed.
    normalized = normalize_night_completion_snapshot(raw) or {}
    for key, value in normalized.items():
        if isinstance(value, bool):
            out[key] = _flag(raw[key])
        elif key in {'deaths', 'engine_deaths', 'blocked', 'guilty_vigs', 'jester_haunts', 'pending_jester_haunts', 'investigative_sent_actor_ids'}:
            out[key] = _ids(raw.get(key))
    if 'day' in out:
        day = _integer(out['day'], minimum=0)
        if day is None:
            out.pop('day')
        else:
            out['day'] = day
    if 'game_key' in out and not isinstance(out['game_key'], (str, int, type(None))):
        out.pop('game_key')
    if 'healed_by' in out:
        pairs = raw.get('healed_by')
        out['healed_by'] = []
        if isinstance(pairs, (list, tuple)):
            for pair in pairs:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    continue
                target, healer = (_integer(value) for value in pair)
                if target is not None and healer is not None:
                    out['healed_by'].append([target, healer])
    if 'protected_by' in out:
        out['protected_by'] = {}
        if isinstance(raw.get('protected_by'), dict):
            for target, guards in raw['protected_by'].items():
                target_id = _integer(target)
                if target_id is None or not isinstance(guards, (list, tuple)):
                    continue
                clean = []
                for guard in guards:
                    if not isinstance(guard, dict) or (guard_id := _integer(guard.get('id'))) is None:
                        continue
                    entry = {'id': guard_id, 'dies_on_guard': _flag(guard.get('dies_on_guard', True))}
                    if 'retri_actor_id' in guard:
                        actor = _integer(guard['retri_actor_id'])
                        if actor is None:
                            continue
                        entry['retri_actor_id'] = actor
                    clean.append(entry)
                out['protected_by'][target_id] = clean
    for key in ('attacked_reasons', 'chaos_visit_targets_by_actor'):
        if key not in out:
            continue
        out[key] = {}
        if isinstance(raw.get(key), dict):
            for actor, value in raw[key].items():
                actor_id = _integer(actor)
                if actor_id is None:
                    continue
                if key == 'attacked_reasons':
                    if isinstance(value, str) and value:
                        out[key][str(actor_id)] = value
                else:
                    out[key][str(actor_id)] = _ids(value)
    return out
