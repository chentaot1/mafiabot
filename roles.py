from typing import Dict, List, Optional, Tuple


# Player-facing role blurbs (mechanics constants live in config.py; behavior in engine/).
ROLE_DESCRIPTIONS: Dict[str, str] = {
    "Retributionist": "> **Faction:** Town (unique)\n> **Stats:** No Attack / No Defense\n> **Goal:** Lynch all evildoers.\n> **Abilities:** **2 uses** (1 at ≤7p). `!corpses`, then `!reanimate <corpse#> <slot>`. Usable Town corpses: Doctor, Sheriff, Investigator, Lookout, Tracker, Escort, Bodyguard, Vigilante, Transporter (two targets; full list from `!corpses`). Not Mayor, Psychic, Deputy, Seer, Scary Grandma, or other Retributionists. Hidden corpses (Gravedigger) unusable; each corpse once. You visit the corpse; the corpse performs the ability (follows **Transporter** like a normal visit). **Escort** corpse roleblocking an aggressive **Serial Killer** can get **you** counter-killed. **Bodyguard** corpse: corpse counters the attack — **you do not die** on guard. **Vigilante** corpse: **guilt** if the shot kills Town. **Doctor** corpse: cannot heal a **revealed Mayor** (use still spent). Roleblock- and Witch-control-immune.",
    "Doctor": "> **Faction:** Town\n> **Stats:** No Attack / No Defense (heal grants **Powerful** defense to target that night)\n> **Goal:** Lynch all evildoers.\n> **Abilities:** `!heal <slot>` nightly. **One self-heal** per game. **No** revealed Mayor.",
    "Sheriff": "> **Faction:** Town\n> **Stats:** No Attack / No Defense\n> **Goal:** Lynch all evildoers.\n> **Abilities:** `!investigate <slot>` → innocent / suspicious. Framed, doused, Mafia, and Arsonist read suspicious.",
    "Investigator": "> **Faction:** Town\n> **Stats:** No Attack / No Defense\n> **Goal:** Lynch all evildoers.\n> **Abilities:** `!investigate <slot>` → role **bucket** (ToS-style). Frames/douses skew buckets like normal investigations.",
    "Lookout": "> **Faction:** Town\n> **Stats:** No Attack / No Defense\n> **Goal:** Lynch all evildoers.\n> **Abilities:** `!watch <slot>` → visitor names (you do not see yourself). **Gatekeeper** guards and **Hypnotist** hypnotizes **count as visits**. Not visits: vest / alert / bg_vest / clean. Chaos/Retributionist corpse `watch` can send visitor DMs without being Lookout.",
    "Tracker": "> **Faction:** Town\n> **Stats:** No Attack / No Defense\n> **Goal:** Lynch all evildoers.\n> **Abilities:** `!track <slot>` → who they visited. **Transporter** and roleblocks can change outcomes.",
    "Vigilante": "> **Faction:** Town\n> **Stats:** Basic Attack / No Defense\n> **Goal:** Lynch all evildoers.\n> **Abilities:** **1** `!shoot <slot>`. **Guilt** (die the **following night**) only if the shot **kills** a role in the Town bucket (`TOWN_ROLES`; not Neutral Benign).",
    "Bodyguard": "> **Faction:** Town\n> **Stats:** ⚔️ Powerful counterattack when guarding / 🛡️ Basic (self-vest + one off-self guard)\n> **Goal:** Lynch all evildoers.\n> **Abilities:** `!protect <slot>`. **One** protect on another player per game, plus **one** self-vest night. On a successful guard vs a kill, you counter with a **Powerful** attack (pierces Basic defense); you may die on guard (`dies_on_guard` rules). If multiple Bodyguards protect the same target, **only the first** counters; others get feedback only.",
    "Escort": "> **Faction:** Town\n> **Stats:** No Attack / No Defense\n> **Goal:** Lynch all evildoers.\n> **Abilities:** `!roleblock <slot>` nightly (visit follows **Transporter**). Cannot roleblock **roleblock-immune** roles. **Serial Killer** (aggressive): immune + may counter-kill you. **Gatekeeper** guards block you as a **Town** visitor; **Consort** (Mafia) is not blocked the same way.",
    "Scary Grandma": "> **Faction:** Town\n> **Stats:** Powerful Attack (on alert) / Basic Defense (on alert)\n> **Goal:** Lynch all evildoers.\n> **Abilities:** **2** `!alert` — visitors die (powerful; pierces basic defense). **Roleblock-** and **control-immune**.",
    "Transporter": "> **Faction:** Town\n> **Stats:** No Attack / No Defense\n> **Goal:** Lynch all evildoers.\n> **Abilities:** `!transport <s1> <s2>` swaps targets for most actions. **Roleblock-** and **control-immune**.",
    "Mayor": "> **Faction:** Town\n> **Stats:** No Attack / No Defense\n> **Goal:** Lynch all evildoers.\n> **Abilities:** Day `!reveal` → vote weight **2**. **Cannot** be healed after reveal.",
    "Mobster": "> **Faction:** Mafia\n> **Stats:** Basic Attack / No Defense\n> **Goal:** Mafia majority.\n> **Abilities:** `!kill` / `/kill` `<slot>` nightly (who holds the kill varies by promotion rules).",
    "Consort": "> **Faction:** Mafia\n> **Stats:** No Attack / No Defense\n> **Goal:** Mafia majority.\n> **Abilities:** `!roleblock <slot>` (visit follows **Transporter**). Same **roleblock-immune** list as Escort. **Serial Killer** (aggressive): immune + may counter-kill you. **Not** blocked by **Gatekeeper** guards on Town targets.",
    "Framer": "> **Faction:** Mafia\n> **Stats:** No Attack / No Defense\n> **Goal:** Mafia majority.\n> **Abilities:** `!frame <slot>` on **Nights 1–2** only. Makes Sheriff/Investigator read suspicious.",
    "Gravedigger": "> **Faction:** Mafia\n> **Stats:** No Attack / No Defense\n> **Goal:** Mafia majority.\n> **Abilities:** **1 use** `!hide <slot>` — if they die, Town may not see their true role.",
    "Hypnotist": "> **Faction:** Mafia\n> **Stats:** No Attack / No Defense\n> **Goal:** Mafia majority.\n> **Abilities:** `!hypnotize <slot> <type>` fake DM. Types: `healed`, `roleblocked`, `transported`, `controlled`, `attacked`. **Counts as visiting** that slot (Lookout / alert).",
    "Mole": "> **Faction:** Mafia\n> **Stats:** No Attack / No Defense\n> **Goal:** Mafia majority.\n> **Abilities:** **1 use** `!investigate <slot>` → exact role (Arsonist/douse overrides apply).",
    "Tailor": "> **Faction:** Mafia\n> **Stats:** No Attack / No Defense\n> **Goal:** Mafia majority.\n> **Abilities:** **1 use** `!tailor <slot> <fake_role>` — death reveal can show fake.",
    "Gatekeeper": "> **Faction:** Mafia\n> **Stats:** No Attack / No Defense\n> **Goal:** Mafia majority.\n> **Abilities:** **2 uses** `!guard <slot>` — **non-Mafia visitors** to that slot can be RB’d (**Consort** not GK-blocked; **Escort** is). Guard follows **Transporter**; cooldown uses the **effective** guarded player. **No** self-guard, **no** Mafia target, **no** guarding the **same** player the night **immediately after** a **successful** guard on them. **Guard counts as visiting** the guarded slot (Lookout / Scary Grandma on alert).",
    "Chaos": "> **Faction:** Neutral (Chaotic)\n> **Stats:** No Attack / **N1 Defense** (first **Basic-tier** night kill only; not ignite)\n> **Goal:** Be **alive** at endgame — you win with **whoever wins** (Town, Mafia, Arsonist, Serial Killer, etc.).\n> **Abilities:** **2** `!chaos <s1> <s2>` — **two other players** (not self). One **secret** random non-kill: **RB, transport, investigate, watch, track, frame, hide, guard** (GK-style on first slot). If you **roleblock** an aggressive **Serial Killer**, you can be **counter-killed** (same as Escort/Consort). You may get investigate/watch/track DMs; effect name is **not** told. Use spent even if no-op / you’re RB’d before resolve.\n> **Notes:** **Control-immune**. **Not** roleblock-immune.",
    "Jester": "> **Faction:** Neutral (Evil)\n> **Stats:** No Attack / **N1 Defense** (first **Basic-tier** kill on **Night 1** only; not ignite)\n> **Goal:** Be **lynched**.\n> **Abilities:** After lynch win → `!haunt` a **guilty** or **abstain** voter. Haunt is applied as a **night death** at resolve (not a normal kill shot, so no heal/BG-style save path).\n> **Notes:** N1 shield is for **lobby Jesters**. **EXE→Jester** after Night 1 effectively has **no** shield (rare early-N1 conversion can still match the engine window).",
    "Executioner": "> **Faction:** Neutral (Evil)\n> **Stats:** No Attack / Basic Defense\n> **Goal:** Get your assigned **Town** (non-Mayor) target **lynched** while you’re alive.\n> **Abilities:** Target **lynched** → you **win** (stay Executioner). Target dies **non-lynch** → you become **Jester**.",
    "Survivor": "> **Faction:** Neutral (Benign)\n> **Stats:** No Attack / Basic while vested\n> **Goal:** Be **alive** at endgame — you win with **whoever wins** (Town, Mafia, Arsonist, Serial Killer, etc.).\n> **Abilities:** **2** `!vest` — blocked = vest not consumed. Vest is **self**-only (not Witch-retargetable).",
    "Witch": "> **Faction:** Neutral (Evil)\n> **Stats:** No Attack / **N1 Defense** (first **Basic-tier** kill; not ignite)\n> **Goal:** Be **alive** when **Town loses** — you joint-win with **Mafia**, **Arsonist**, or **Serial Killer** (not on a **Town** win; unlike **Survivor**, who wins with any side).\n> **Abilities:** `!control <victim> <newTarget>` — learn victim’s role; redirect their action when rules allow. Cannot retarget self-only actions (`vest`, `clean`).\n> **Notes:** **Roleblock-immune** and **control-immune**.",
    "Pirate": "> **Faction:** Neutral (Evil)\n> **Stats:** Powerful Attack (on duel win) / No Defense\n> **Goal:** **2** duel wins that **also kill** (plunder kill must land).\n> **Abilities:** `!plunder <slot>` — RPS duel; RB target win or lose; **Powerful** kill only on **win**. **Roleblock-immune**; Gatekeeper on target can still block you as a visitor.",
    "Arsonist": "> **Faction:** Neutral (Killing)\n> **Stats:** Unstoppable ignite / Basic vs normal kills\n> **Goal:** Eliminate opposition.\n> **Abilities:** `!douse`, `!doused` (list doused), `!ignite`, `!clean` (self). Doused + you read suspicious. **Ignite** is Unstoppable (pierces heal/Basic/Powerful); **GA invincible ward** still blocks.",
    "Guardian Angel": "> **Faction:** Neutral (Benign)\n> **Stats:** No Attack / No Defense (`!ward` grants **invincible** defense on bind that night only)\n> **Goal:** You and your bound player must survive and your bind must achieve their win; a stalemate override can also qualify. A dead GA may still ward but does not win.\n> **Abilities:** Start bound to one other player (DM), including neutrals. **1×** `!ward <bind slot>` — clears their douse, **invincible** ward that night (blocks kills and ignite), locks **nominations** on them next day if they would be on trial, public dawn line. **Living** ward is a physical visit; **dead GA** ward is **astral** (no Lookout/GK/SG alert). Defeated if bind dies (except protected lynch day) or bind is haunt-killed.",
    "Serial Killer": "> **Faction:** Neutral (Killing)\n> **Stats:** Basic Attack / Basic Defense vs normal kills\n> **Goal:** Last killer standing.\n> **Abilities:** `!stab <slot>` nightly (not the Mafia `!kill`). `!cautious` toggles **Aggressive** (counter Escort/Consort who roleblock you) vs **Cautious**. Immune to roleblock; Pirate duel interactions apply.",
    "Psychic": "> **Faction:** Town\n> **Stats:** No Attack / No Defense\n> **Goal:** Lynch all evildoers.\n> **Abilities:** **Passive** visions after each **resolve** (odd: 1 evil among 3 slots; even: 1 good among 2). **Roleblocked** = no vision. Witch control can **steal** the vision text.",
    "Deputy": "> **Faction:** Town\n> **Stats:** Unstoppable Attack (day) / No Defense\n> **Goal:** Lynch all evildoers.\n> **Abilities:** From **Day 2**, **1** daytime `!shoot <slot>` from **DM or private player channel**. Gun reads framed/doused/Mafia/killing neutrals as evil. **Unstoppable** pierces Basic/Powerful passive defense (e.g. Executioner, SK, Arsonist); wrong shot kills target then you. **One bullet per Deputy** for the entire game.",
    "Seer": "> **Faction:** Town\n> **Stats:** No Attack / No Defense\n> **Goal:** Lynch all evildoers.\n> **Abilities:** `!gaze <s1> <s2>` — **Friends** vs **Enemies** from bucketed alignment (GA/Jester/Town bucket; Mafia bucket; NK bucket; hostile neutrals). **Tailor** fake death roles do **not** affect gaze. Revealed Mayor cannot be gazed. **Gatekeeper / roleblock** cancels gaze. **Witch** on an idle Seer forces both gaze slots to the Witch's target (useless **Friends** self-pair); if the Seer already picked targets, only the **first** slot is overwritten.",
}


def get_role_description(role: str) -> str:
    return ROLE_DESCRIPTIONS.get(role, "You seem to have a role that defies description.")


def role_start_dm_supplements(
    role: str,
    *,
    bind_slot: Optional[int] = None,
    exe_target_display: Optional[str] = None,
) -> List[Tuple[str, str]]:
    """
    Optional extra game-start DMs after the generic role_deal message.
    Returns (outbox_kind, content) pairs.
    """
    out: List[Tuple[str, str]] = []
    if role == "Executioner" and exe_target_display:
        out.append(
            (
                "exe_target",
                f"Your target is **{exe_target_display}**. You must convince the Town to lynch them to win.",
            )
        )
    if role == "Guardian Angel" and bind_slot is not None:
        out.append(
            (
                "ga_bind",
                (
                    f"You are mystically bound to **Slot {bind_slot}**.\n"
                    f"**Command:** `!ward {bind_slot}` (only your bind; **1 charge** for the whole game).\n"
                    f"**Ward:** clears douse, blocks kills and ignite that night, and prevents their nomination "
                    f"to trial the next day. Town will see a public line at dawn (your name is not revealed).\n"
                    f"If you die, you may still submit `!ward` from the grave (**astral** — invisible to Lookout; "
                    f"immune to SG alert and Gatekeeper). While alive, roleblock stops your ward."
                ),
            )
        )
    if role == "Psychic":
        out.append(
            (
                "psychic_brief",
                (
                    "**Psychic — passive visions**\n"
                    "You have **no night command**. After the GM runs `!resolve`, you may receive a vision DM.\n"
                    "• **Odd nights:** one of **three slot numbers** is evil (Mafia, listed neutrals, framed, or doused).\n"
                    "• **Even nights:** one of **two slot numbers** is good (Town or Survivor).\n"
                    "If you are **roleblocked**, you get no vision (and the Witch cannot steal one that night)."
                ),
            )
        )
    if role == "Seer":
        out.append(
            (
                "seer_brief",
                (
                    "**Seer — compare two players**\n"
                    "**Command:** `!gaze <slot1> <slot2>` each night (private channel or DM).\n"
                    "You learn **Friends** (same alignment bucket) or **Enemies**.\n"
                    "You cannot gaze a **revealed Mayor**. Each unordered pair only once per game.\n"
                    "**Roleblock** or **Gatekeeper** blocking your visit cancels the gaze."
                ),
            )
        )
    if role == "Deputy":
        out.append(
            (
                "deputy_brief",
                (
                    "**Deputy — daytime revolver**\n"
                    "From **Day 2** onward, use `!shoot <slot>` during the **day** in your private channel or DM.\n"
                    "• **One bullet** for the entire game.\n"
                    "• Each Deputy may fire **once** for the whole game.\n"
                    "Your gun treats Mafia, framed/doused players, and killing neutrals as **evil**.\n"
                    "Shooting true Town kills them, then **you die** from the mistake."
                ),
            )
        )
    if role == "Serial Killer":
        out.append(
            (
                "sk_brief",
                (
                    "**Serial Killer**\n"
                    "**Command:** `!stab <slot>` each night (use `!stab`, **not** Mafia `!kill`).\n"
                    "**`!cautious`** toggles **Cautious** vs **Aggressive**:\n"
                    "• **Aggressive:** Escort/Consort who roleblock you may be **counter-killed**.\n"
                    "• **Cautious:** roleblockers are spared from counters.\n"
                    "You are **roleblock-immune** (blocks still notify you). **Guardian Angel** wards block your stab."
                ),
            )
        )
    return out


def role_start_private_channel_lines(
    role: str,
    *,
    bind_slot: Optional[int] = None,
    exe_target_display: Optional[str] = None,
) -> List[str]:
    """Extra lines appended to the game-start post in the player's private channel."""
    lines: List[str] = []
    if role == "Executioner" and exe_target_display:
        lines.append(f"**Executioner target:** **{exe_target_display}** — get the Town to lynch them.")
    if role == "Guardian Angel" and bind_slot is not None:
        lines.append(
            f"**Bind:** **Slot {bind_slot}** — `!ward {bind_slot}` only (**1×**). Clears douse, blocks kills/ignite, "
            f"locks their nomination next day."
        )
    if role == "Psychic":
        lines.append(
            "**Psychic:** passive visions after each `!resolve` (odd = evil among 3 slots, even = good among 2). "
            "No night command."
        )
    if role == "Seer":
        lines.append("**Seer:** `!gaze <slot1> <slot2>` each night → Friends or Enemies.")
    if role == "Deputy":
        lines.append(
            "**Deputy:** from **Day 2**, `!shoot <slot>` in this channel or DM (**1 bullet** total; "
            "**one town revolver per day**)."
        )
    if role == "Serial Killer":
        lines.append("**Serial Killer:** `!stab <slot>` nightly; `!cautious` toggles counter-attacks on roleblockers.")
    return lines
