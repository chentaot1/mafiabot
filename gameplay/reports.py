"""Private report history containing only feedback addressed to its owner."""
from copy import deepcopy


def history(game, uid):
    raw = game.role_states.get(uid, {}).get('private_reports', [])
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)
            and entry.get('version') == 1 and entry.get('match') == game.game_key
            and entry.get('owner') == uid and isinstance(entry.get('owner'), int) and not isinstance(entry['owner'], bool)
            and isinstance(entry.get('kind'), str) and entry['kind'] in {'seer', 'psychic'}
            and isinstance(entry.get('night'), int) and not isinstance(entry['night'], bool)
            and 1 <= entry['night'] <= game.day_number
            and isinstance(entry.get('text'), str) and len(entry['text']) <= 1000
            and isinstance(entry.get('selected'), list) and len(entry['selected']) <= 2
            and all(isinstance(pid, int) and not isinstance(pid, bool) for pid in entry['selected'])]


def remember(game, uid, text, *, kind, source_id=None, status='result', selected=()):
    """Called on the isolated resolution model; application saves this with results."""
    source_id = uid if source_id is None else source_id
    entries = history(game, uid)
    key = (game.day_number, kind, source_id)
    if any((entry['night'], entry['kind'], entry.get('source')) == key for entry in entries):
        return
    entry = {'version': 1, 'match': game.game_key, 'owner': uid, 'night': game.day_number,
             'kind': kind, 'source': source_id, 'status': status, 'text': text,
             'selected': [pid for pid in selected if isinstance(pid, int) and not isinstance(pid, bool)]}
    game.role_states.setdefault(uid, {})['private_reports'] = entries + [entry]


def for_feedback(game, uid, text, night):
    return next((deepcopy(entry) for entry in reversed(history(game, uid))
                 if entry['night'] == night and entry['text'] == text), None)
