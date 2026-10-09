"""Versioned JSON recovery boundaries. Unusable records fall back to legacy cleanup."""
from copy import deepcopy
from datetime import datetime


def integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 32


def deadline(value):
    try:
        return isinstance(value, str) and datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def reference(value):
    return (isinstance(value, dict) and integer(value.get('channel_id'))
            and integer(value.get('message_id')))


def load_ui(raw):
    result = {'version':1, 'trial':None, 'panels':{}, 'night_token':None, 'deaths':{}, 'duels':{}}
    if not isinstance(raw, dict) or raw.get('version') != 1:
        return result
    token = raw.get('night_token')
    if identifier(token):
        result['night_token']=token
    startup = raw.get('startup')
    if isinstance(startup, dict) and isinstance(startup.get('match'), str):
        completed = startup.get('completed_players', [])
        result['startup'] = {'match': startup['match'], 'complete': startup.get('complete') is True,
            'announced': startup.get('announced') is True,
            'mafia_announced': startup.get('mafia_announced') is True,
            'completed_players': [uid for uid in completed if integer(uid)] if isinstance(completed, list) else []}
        receipts = startup.get('dm_receipts', {})
        result['startup']['dm_receipts'] = {
            str(uid): list(dict.fromkeys(kind for kind in kinds if identifier(kind)))
            for uid, kinds in receipts.items() if str(uid).isdigit() and isinstance(kinds, list)
        } if isinstance(receipts, dict) else {}
    duels=raw.get('duels')
    if isinstance(duels,dict):
        for key,action in duels.items():
            if (identifier(key) and isinstance(action,dict) and action.get('type')=='plunder'
                    and action.get('duel_finished') is True and action.get('duel_token')==key
                    and integer(action.get('actor')) and integer(action.get('target')) and deadline(action.get('duel_deadline'))):
                action=deepcopy(action)
                repair_duel(action,day=action.get('duel_day'),match=action.get('duel_match'))
                result['duels'][key]=action
    panels=raw.get('panels')
    if isinstance(panels,dict):
        result['panels']={str(k):deepcopy(v) for k,v in panels.items() if str(k).isdigit() and reference(v)}
    deaths=raw.get('deaths')
    if isinstance(deaths,dict):
        for uid,receipt in deaths.items():
            if not isinstance(receipt,dict) or not integer(receipt.get('player_id')) or str(receipt['player_id']) != str(uid):
                continue
            if not all(isinstance(receipt.get(k),str) for k in ('real_role','revealed_role','cause','will')):
                continue
            if not all(isinstance(receipt.get(k),list) and all(integer(x) for x in receipt[k]) for k in ('voters','converted')):
                continue
            saved = deepcopy(receipt)
            for key in ('delivered', 'access_cleaned', 'notices_delivered'):
                saved[key] = saved.get(key) is True
            recipients = set(saved['converted'])
            if saved['real_role'] == 'Jester' and saved['cause'] == 'lynch' and saved['voters']:
                recipients.add(saved['player_id'])
            completed = saved.get('notice_delivered_ids', [])
            saved['notice_delivered_ids'] = list(dict.fromkeys(
                uid for uid in completed if integer(uid) and uid in recipients)) if isinstance(completed, list) else []
            if not integer(saved.get('announcement_id')):
                saved['announcement_id'] = None
            result['deaths'][str(uid)] = saved
    trial=raw.get('trial')
    if isinstance(trial,dict) and identifier(trial.get('id')) and integer(trial.get('day')):
        stage=trial.get('stage')
        valid=isinstance(stage,str) and stage in {'nomination','defense','judgment','closed','done','cancelled'}
        valid &= all(isinstance(trial.get(k),dict) for k in ('nominations','judgments'))
        valid &= integer(trial.get('channel_id'))
        if isinstance(stage,str) and stage in {'nomination','defense','judgment'}:
            valid &= deadline(trial.get('deadline'))
        if isinstance(stage,str) and stage in {'defense','judgment','closed'}:
            valid &= integer(trial.get('defendant'))
        verdict=trial.get('result')
        if stage=='closed' or verdict is not None:
            valid &= isinstance(verdict,dict)
            if isinstance(verdict,dict):
                choices,weights,totals=verdict.get('choices'),verdict.get('weights'),verdict.get('totals')
                valid &= isinstance(choices,dict) and isinstance(weights,dict) and isinstance(totals,dict)
                if isinstance(choices,dict) and isinstance(weights,dict) and isinstance(totals,dict):
                    valid &= set(choices)==set(weights)
                    valid &= all(str(k).isdigit() and isinstance(v,str) and v in {'guilty','innocent','abstain'} for k,v in choices.items())
                    valid &= all(integer(v) and v in {1,2} for v in weights.values())
                    valid &= all(integer(totals.get(k)) and totals[k]>=0 for k in ('guilty','innocent','abstain'))
                    valid &= isinstance(verdict.get('haunt_ids'),list) and all(integer(x) for x in verdict.get('haunt_ids',[]))
                    valid &= isinstance(verdict.get('guilty'),bool)
        if valid:
            trial=deepcopy(trial)
            trial['nominations']={str(k):v for k,v in trial['nominations'].items() if str(k).isdigit() and (v is None or integer(v))}
            trial['judgments']={str(k):v for k,v in trial['judgments'].items() if str(k).isdigit() and isinstance(v,str) and v in {'guilty','innocent','abstain'}}
            for key in ('applied','progressed','counted','refunded','permissions_cleaned'):
                trial[key]=trial.get(key) is True
            page=trial.get('display_page',0)
            trial['display_page']=max(0,page) if integer(page) else 0
            result['trial']=trial
    result['trials'] = {}
    archives = raw.get('trials', {})
    if isinstance(archives, dict):
        for key, saved in archives.items():
            if (identifier(key) and isinstance(saved, dict) and saved.get('id') == key
                    and saved.get('stage') in ('done', 'cancelled')):
                validated = load_ui({'version': 1, 'trial': saved})['trial']
                if validated:
                    result['trials'][key] = validated
    if result['trial'] and result['trial']['id'] in result['trials']:
        result['trials'][result['trial']['id']] = result['trial']
    result['resolutions'] = {}
    records = raw.get('resolutions', {})
    records = dict(records) if isinstance(records, dict) else {}
    current = raw.get('resolution')
    if isinstance(current, dict) and identifier(current.get('night_token')):
        records[current.get('night_token')] = current
    for record in records.values():
        if (isinstance(record,dict) and identifier(record.get('night_token')) and record.get('applied') is True
                and (record.get('night_token')==token or record.get('progressed') is True)):
            feedback=record.get('feedback'); index=record.get('feedback_index'); ids=record.get('death_ids')
            if (isinstance(feedback,list) and all(isinstance(x,dict) and integer(x.get('user_id')) and isinstance(x.get('text'),str) for x in feedback)
                    and integer(index) and 0<=index<=len(feedback) and isinstance(ids,list) and all(integer(x) for x in ids)):
                saved=deepcopy(record)
                saved['progressed']=record.get('progressed') is True
                saved['public_delivery_pending']=record.get('public_delivery_pending') is True
                result['resolutions'][saved['night_token']]=saved
    if isinstance(current, dict) and identifier(current.get('night_token')) and current.get('night_token') in result['resolutions']:
        result['resolution'] = result['resolutions'][current['night_token']]
    return result


def repair_duel(action, *, day, match):
    if not isinstance(action, dict):
        return
    if action.get('type') != 'plunder':
        return
    modern=(identifier(action.get('duel_token')) and deadline(action.get('duel_deadline'))
            and integer(action.get('duel_day')) and action.get('duel_day')==day
            and isinstance(action.get('duel_match'),str) and action.get('duel_match')==match
            and integer(action.get('actor')) and integer(action.get('target')))
    if not modern:
        won=action.get('duel_won') is True and action.get('duel_finished') is True
        action.update(duel_finished=True,duel_won=won)
        return
    if not isinstance(action.get('duel_finished'),bool) or (action.get('duel_finished') is True and not isinstance(action.get('duel_won'),bool)):
        action.update(duel_finished=True,duel_won=False,duel_result='The duel was cancelled because its saved result was invalid.')
    choices=action.get('duel_choices',{})
    action['duel_choices']={str(k):v for k,v in choices.items() if str(k).isdigit() and isinstance(v,str) and v in {'rock','paper','scissors'}} if isinstance(choices,dict) else {}
    prompts=action.get('duel_prompts',{})
    action['duel_prompts']={str(k):deepcopy(v) for k,v in prompts.items() if str(k).isdigit() and reference(v)} if isinstance(prompts,dict) else {}
    delivered=action.get('duel_delivered',[])
    action['duel_delivered']=[x for x in delivered if integer(x)] if isinstance(delivered,list) else []
