"""Consigliere-style two-line blurbs (Spec 6)."""

from __future__ import annotations

ROLE_CONSIG_BLURB: dict[str, str] = {
    "Bodyguard": "Your target is a trained protector.\nThey must be a Bodyguard.",
    "Doctor": "Your target is a professional surgeon.\nThey must be a Doctor.",
    "Escort": "Your target is a beautiful person working for the town.\nThey must be an Escort.",
    "Investigator": "Your target gathers information about people.\nThey must be an Investigator.",
    "Lookout": "Your target watches who visits people at night.\nThey must be a Lookout.",
    "Mayor": "Your target is the leader of the town.\nThey must be the Mayor.",
    "Retributionist": "Your target wields mystical powers.\nThey must be a Retributionist.",
    "Sheriff": "Your target is a protector of the town.\nThey must be a Sheriff.",
    "Transporter": "Your target specializes in transportation.\nThey must be a Transporter.",
    "Scary Grandma": "Your target is a paranoid war hero.\nThey must be a Scary Grandma.",
    "Vigilante": "Your target will bend the law to enact justice.\nThey must be a Vigilante.",
    "Mole": "Your target gathers information for the Mafia.\nThey must be a Mole.",
    "Consort": "Your target is a beautiful person working for the Mafia.\nThey must be a Consort.",
    "Tailor": "Your target is good at forging documents.\nThey must be a Tailor.",
    "Framer": "Your target has a desire to deceive.\nThey must be a Framer.",
    "Hypnotist": "Your target is skilled at disrupting others.\nThey must be a Hypnotist.",
    "Gravedigger": "Your target cleans up dead bodies.\nThey must be a Gravedigger.",
    "Mobster": "Your target does the Godfather's dirty work.\nThey must be a Mobster.",
    "Arsonist": "Your target likes to watch things burn.\nThey must be an Arsonist.",
    "Executioner": "Your target wants someone to be lynched at any cost.\nThey must be an Executioner.",
    "Jester": "Your target wants to be lynched.\nThey must be a Jester.",
    "Serial Killer": "Your target wants to kill everyone.\nThey must be a Serial Killer.",
    "Witch": "Your target casts spells on people.\nThey must be a Witch.",
    "Survivor": "Your target simply wants to live.\nThey must be a Survivor.",
    "Psychic": "Your target has the sight.\nThey must be a Psychic.",
    "Guardian Angel": "Your target is watching over someone.\nThey must be a Guardian Angel.",
    "Pirate": "Your target wants to plunder the town.\nThey must be a Pirate.",
    "Tracker": "Your target is skilled in the art of tracking.\nThey must be a Tracker.",
    "Deputy": "Your target serves the law in broad daylight.\nThey must be a Deputy.",
    "Seer": "Your target reads the heavens for signs of allegiance.\nThey must be a Seer.",
    "Gatekeeper": "Your target seals the doors against unwanted guests.\nThey must be a Gatekeeper.",
    "Chaos": "Your target spreads uncertainty and mischief.\nThey must be Chaos.",
}


def consig_blurb(role: str) -> str:
    return ROLE_CONSIG_BLURB.get(role, f"Your target is mysterious.\nThey must be a {role}.")
