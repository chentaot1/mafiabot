# MafiaBot — Player and Role Guide

MafiaBot runs an overseer-led social deduction game on Discord. There are 32 configured roles: 15 Town, 8 Mafia, and 9 neutral. Players use stable numbered seats, so a death never changes another player's target number.

## Starting and playing

Join with `!join`; the overseer starts with `!startgame`. The game checks that players can receive DMs before dealing roles. Players should keep DMs enabled for role assignments and private feedback. The bot creates or reuses the game's text/voice channels and roles; server permissions must be configured correctly.

Day is for discussion and tribunal voting. The overseer starts a trial with `!vote`; private nomination controls lead to defense, judgment, and a verdict. A revealed Mayor has double vote weight. A tie or no nomination spares the town from that trial. There are at most two trials per day.

At night, use `/actions` to open your private panel. Choose an ability, select its targets, and press Submit. Nothing in a draft is saved before submission. Prefix commands also work in DMs; supported hybrid commands work in verified private player channels. Public role-action submissions are rejected. Reopening `/actions` gives the current phase's controls if an old panel has expired.

The overseer resolves with `!resolve`. Valid resubmissions replace earlier actions until resolution freezes them. Pirate duels must finish first. Delayed guilt, conversions, and faction/personal objectives can change the outcome after an action was accepted.

## Lobby composition and resources

Five-player games use one Investigative Town, one Protective Town, two Random Town, and a Mobster. Six/seven-player games add one/two neutral slots. Larger games use the weighted pools described in the README. Roles can repeat except Mayor, Scary Grandma, Retributionist, Mobster, Pirate, Arsonist, and Guardian Angel.

Retributionist, Survivor, Scary Grandma, Gatekeeper, and Chaos start with one charge in games of seven or fewer players and two above seven. Other quantities in the role cards below apply as written. When a card says two charges, the small-lobby adjustment still applies.

`!myrole` gives your role card in a DM. `!players` lists seat numbers; `!will` opens or clears your will, subject to the current game's eligibility checks. `/leaderboard` and `!stats` show recorded results. Neutral personal victories are separate from faction outcomes.

## Role roster


### Town


#### Bodyguard

**Faction:** Town
**Stats:** ⚔️ Powerful counterattack when guarding / 🛡️ Basic (self-vest + one off-self guard)
**Goal:** Lynch all evildoers.
**Abilities:** `!protect <slot>`. **One** protect on another player per game, plus **one** self-vest night. On a successful guard vs a kill, you counter with a **Powerful** attack (pierces Basic defense); you may die on guard (`dies_on_guard` rules). If multiple Bodyguards protect the same target, **only the first** counters; others get feedback only.


#### Deputy

**Faction:** Town
**Stats:** Unstoppable Attack (day) / No Defense
**Goal:** Lynch all evildoers.
**Abilities:** From **Day 2**, **1** daytime `!shoot <slot>` from **DM or private player channel**. Gun reads framed/doused/Mafia/killing neutrals as evil. **Unstoppable** pierces Basic/Powerful passive defense (e.g. Executioner, SK, Arsonist); wrong shot kills target then you. **One bullet per Deputy** for the entire game.


#### Doctor

**Faction:** Town
**Stats:** No Attack / No Defense (heal grants **Powerful** defense to target that night)
**Goal:** Lynch all evildoers.
**Abilities:** `!heal <slot>` nightly. **One self-heal** per game. **No** revealed Mayor.


#### Escort

**Faction:** Town
**Stats:** No Attack / No Defense
**Goal:** Lynch all evildoers.
**Abilities:** `!roleblock <slot>` nightly (visit follows **Transporter**). Cannot roleblock **roleblock-immune** roles. **Serial Killer** (aggressive): immune + may counter-kill you. **Gatekeeper** guards block you as a **Town** visitor; **Consort** (Mafia) is not blocked the same way.


#### Investigator

**Faction:** Town
**Stats:** No Attack / No Defense
**Goal:** Lynch all evildoers.
**Abilities:** `!investigate <slot>` → role **bucket** (ToS-style). Frames/douses skew buckets like normal investigations.


#### Lookout

**Faction:** Town
**Stats:** No Attack / No Defense
**Goal:** Lynch all evildoers.
**Abilities:** `!watch <slot>` → visitor names (you do not see yourself). **Gatekeeper** guards and **Hypnotist** hypnotizes **count as visits**. Not visits: vest / alert / bg_vest / clean. Chaos/Retributionist corpse `watch` can send visitor DMs without being Lookout.


#### Mayor

**Faction:** Town
**Stats:** No Attack / No Defense
**Goal:** Lynch all evildoers.
**Abilities:** Day `!reveal` → vote weight **2**. **Cannot** be healed after reveal.


#### Psychic

**Faction:** Town
**Stats:** No Attack / No Defense
**Goal:** Lynch all evildoers.
**Abilities:** **Passive** visions after each **resolve** (odd: 1 evil among 3 slots; even: 1 good among 2). **Roleblocked** = no vision. Witch control can **steal** the vision text.


#### Retributionist

**Faction:** Town (unique)
**Stats:** No Attack / No Defense
**Goal:** Lynch all evildoers.
**Abilities:** **2 uses** (1 at ≤7p). `!corpses`, then `!reanimate <corpse#> <slot>`. Usable Town corpses: Doctor, Sheriff, Investigator, Lookout, Tracker, Escort, Bodyguard, Vigilante, Transporter (two targets; full list from `!corpses`). Not Mayor, Psychic, Deputy, Seer, Scary Grandma, or other Retributionists. Hidden corpses (Gravedigger) unusable; each corpse once. You visit the corpse; the corpse performs the ability (follows **Transporter** like a normal visit). **Escort** corpse roleblocking an aggressive **Serial Killer** can get **you** counter-killed. **Bodyguard** corpse: corpse counters the attack — **you do not die** on guard. **Vigilante** corpse: **guilt** if the shot kills Town. **Doctor** corpse: cannot heal a **revealed Mayor** (use still spent). Roleblock- and Witch-control-immune.


#### Scary Grandma

**Faction:** Town
**Stats:** Powerful Attack (on alert) / Basic Defense (on alert)
**Goal:** Lynch all evildoers.
**Abilities:** **2** `!alert` — visitors die (powerful; pierces basic defense). **Roleblock-** and **control-immune**.


#### Seer

**Faction:** Town
**Stats:** No Attack / No Defense
**Goal:** Lynch all evildoers.
**Abilities:** `!gaze <s1> <s2>` — **Friends** vs **Enemies** from bucketed alignment (GA/Jester/Town bucket; Mafia bucket; NK bucket; hostile neutrals). **Tailor** fake death roles do **not** affect gaze. Revealed Mayor cannot be gazed. **Gatekeeper / roleblock** cancels gaze. **Witch** on an idle Seer forces both gaze slots to the Witch's target (useless **Friends** self-pair); if the Seer already picked targets, only the **first** slot is overwritten.


#### Sheriff

**Faction:** Town
**Stats:** No Attack / No Defense
**Goal:** Lynch all evildoers.
**Abilities:** `!investigate <slot>` → innocent / suspicious. Framed, doused, Mafia, and Arsonist read suspicious.


#### Tracker

**Faction:** Town
**Stats:** No Attack / No Defense
**Goal:** Lynch all evildoers.
**Abilities:** `!track <slot>` → who they visited. **Transporter** and roleblocks can change outcomes.


#### Transporter

**Faction:** Town
**Stats:** No Attack / No Defense
**Goal:** Lynch all evildoers.
**Abilities:** `!transport <s1> <s2>` swaps targets for most actions. **Roleblock-** and **control-immune**.


#### Vigilante

**Faction:** Town
**Stats:** Basic Attack / No Defense
**Goal:** Lynch all evildoers.
**Abilities:** **1** `!shoot <slot>`. **Guilt** (die the **following night**) only if the shot **kills** a role in the Town bucket (`TOWN_ROLES`; not Neutral Benign).


### Mafia


#### Consort

**Faction:** Mafia
**Stats:** No Attack / No Defense
**Goal:** Mafia majority.
**Abilities:** `!roleblock <slot>` (visit follows **Transporter**). Same **roleblock-immune** list as Escort. **Serial Killer** (aggressive): immune + may counter-kill you. **Not** blocked by **Gatekeeper** guards on Town targets.


#### Framer

**Faction:** Mafia
**Stats:** No Attack / No Defense
**Goal:** Mafia majority.
**Abilities:** `!frame <slot>` on **Nights 1–2** only. Makes Sheriff/Investigator read suspicious.


#### Gatekeeper

**Faction:** Mafia
**Stats:** No Attack / No Defense
**Goal:** Mafia majority.
**Abilities:** **2 uses** `!guard <slot>` — **non-Mafia visitors** to that slot can be RB’d (**Consort** not GK-blocked; **Escort** is). Guard follows **Transporter**; cooldown uses the **effective** guarded player. **No** self-guard, **no** Mafia target, **no** guarding the **same** player the night **immediately after** a **successful** guard on them. **Guard counts as visiting** the guarded slot (Lookout / Scary Grandma on alert).


#### Gravedigger

**Faction:** Mafia
**Stats:** No Attack / No Defense
**Goal:** Mafia majority.
**Abilities:** **1 use** `!hide <slot>` — if they die, Town may not see their true role.


#### Hypnotist

**Faction:** Mafia
**Stats:** No Attack / No Defense
**Goal:** Mafia majority.
**Abilities:** `!hypnotize <slot> <type>` fake DM. Types: `healed`, `roleblocked`, `transported`, `controlled`, `attacked`. **Counts as visiting** that slot (Lookout / alert).


#### Mobster

**Faction:** Mafia
**Stats:** Basic Attack / No Defense
**Goal:** Mafia majority.
**Abilities:** `!kill` / `/kill` `<slot>` nightly (who holds the kill varies by promotion rules).


#### Mole

**Faction:** Mafia
**Stats:** No Attack / No Defense
**Goal:** Mafia majority.
**Abilities:** **1 use** `!investigate <slot>` → exact role (Arsonist/douse overrides apply).


#### Tailor

**Faction:** Mafia
**Stats:** No Attack / No Defense
**Goal:** Mafia majority.
**Abilities:** **1 use** `!tailor <slot> <fake_role>` — death reveal can show fake.


### Neutral


#### Arsonist

**Faction:** Neutral (Killing)
**Stats:** Unstoppable ignite / Basic vs normal kills
**Goal:** Eliminate opposition.
**Abilities:** `!douse`, `!doused` (list doused), `!ignite`, `!clean` (self). Doused + you read suspicious. **Ignite** is Unstoppable (pierces heal/Basic/Powerful); **GA invincible ward** still blocks.


#### Chaos

**Faction:** Neutral (Chaotic)
**Stats:** No Attack / **N1 Defense** (first **Basic-tier** night kill only; not ignite)
**Goal:** Be **alive** at endgame — you win with **whoever wins** (Town, Mafia, Arsonist, Serial Killer, etc.).
**Abilities:** **2** `!chaos <s1> <s2>` — **two other players** (not self). One **secret** random non-kill: **RB, transport, investigate, watch, track, frame, hide, guard** (GK-style on first slot). If you **roleblock** an aggressive **Serial Killer**, you can be **counter-killed** (same as Escort/Consort). You may get investigate/watch/track DMs; effect name is **not** told. Use spent even if no-op / you’re RB’d before resolve.
**Notes:** **Control-immune**. **Not** roleblock-immune.


#### Executioner

**Faction:** Neutral (Evil)
**Stats:** No Attack / Basic Defense
**Goal:** Get your assigned **Town** (non-Mayor) target **lynched** while you’re alive.
**Abilities:** Target **lynched** → you **win** (stay Executioner). Target dies **non-lynch** → you become **Jester**.


#### Guardian Angel

**Faction:** Neutral (Benign)
**Stats:** No Attack / No Defense (`!ward` grants **invincible** defense on bind that night only)
**Goal:** You and your bound player must survive and your bind must achieve their win; a stalemate override can also qualify. A dead GA may still ward but does not win.
**Abilities:** Start bound to one other player (DM), including neutrals. **1×** `!ward <bind slot>` — clears their douse, **invincible** ward that night (blocks kills and ignite), locks **nominations** on them next day if they would be on trial, public dawn line. **Living** ward is a physical visit; **dead GA** ward is **astral** (no Lookout/GK/SG alert). Defeated if bind dies (except protected lynch day) or bind is haunt-killed.


#### Jester

**Faction:** Neutral (Evil)
**Stats:** No Attack / **N1 Defense** (first **Basic-tier** kill on **Night 1** only; not ignite)
**Goal:** Be **lynched**.
**Abilities:** After lynch win → `!haunt` a **guilty** or **abstain** voter. Haunt is applied as a **night death** at resolve (not a normal kill shot, so no heal/BG-style save path).
**Notes:** N1 shield is for **lobby Jesters**. **EXE→Jester** after Night 1 effectively has **no** shield (rare early-N1 conversion can still match the engine window).


#### Pirate

**Faction:** Neutral (Evil)
**Stats:** Powerful Attack (on duel win) / No Defense
**Goal:** **2** duel wins that **also kill** (plunder kill must land).
**Abilities:** `!plunder <slot>` — RPS duel; RB target win or lose; **Powerful** kill only on **win**. **Roleblock-immune**; Gatekeeper on target can still block you as a visitor.


#### Serial Killer

**Faction:** Neutral (Killing)
**Stats:** Basic Attack / Basic Defense vs normal kills
**Goal:** Last killer standing.
**Abilities:** `!stab <slot>` nightly (not the Mafia `!kill`). `!cautious` toggles **Aggressive** (counter Escort/Consort who roleblock you) vs **Cautious**. Immune to roleblock; Pirate duel interactions apply.


#### Survivor

**Faction:** Neutral (Benign)
**Stats:** No Attack / Basic while vested
**Goal:** Be **alive** at endgame — you win with **whoever wins** (Town, Mafia, Arsonist, Serial Killer, etc.).
**Abilities:** **2** `!vest` — blocked = vest not consumed. Vest is **self**-only (not Witch-retargetable).


#### Witch

**Faction:** Neutral (Evil)
**Stats:** No Attack / **N1 Defense** (first **Basic-tier** kill; not ignite)
**Goal:** Be **alive** when **Town loses** — you joint-win with **Mafia**, **Arsonist**, or **Serial Killer** (not on a **Town** win; unlike **Survivor**, who wins with any side).
**Abilities:** `!control <victim> <newTarget>` — learn victim’s role; redirect their action when rules allow. Cannot retarget self-only actions (`vest`, `clean`).
**Notes:** **Roleblock-immune** and **control-immune**.


## Questions and interaction rules

**Does accepting an action guarantee it will work?** No. Transport, control, roleblock, protection, and simultaneous deaths can change its effect. The ordered engine determines the final result.

**What counts as a visit?** Ordinary targeted actions visit their effective destinations. Gatekeeper guards and Hypnotist actions count as visits. Self-only vest, alert, Bodyguard vest, and clean do not visit another house. Transport/control have special visit rules. A dead Guardian Angel wards astrally and does not appear as a visitor.

**Can a Doctor stop ignite?** No: Doctor healing grants powerful defense, while ignite is unstoppable. Guardian Angel's invincible ward can stop ignite and also clears the bind's douse.

**When does Vigilante guilt apply?** Only after actually killing Town. A failed attack does not trigger guilt. The resulting death is deferred to the following night. A mistaken Deputy shot is different: the Deputy dies as part of the daytime shot.

**Can I use a Transporter corpse?** Yes. The modern controls accept two distinct living targets and the shared corpse-expansion code creates a real transport action. Eligible corpse types are Doctor, Sheriff, Investigator, Lookout, Tracker, Escort, Bodyguard, Vigilante, and Transporter. A hidden or already-used corpse is unavailable; Psychic, Deputy, Seer, Mayor, Scary Grandma, and Retributionist are not usable corpse types.

**Can a dead player act?** Normally no. A successfully lynched Jester may haunt an eligible guilty/abstaining voter. A Guardian Angel can use a remaining ward from the grave while the bound player remains eligible, but a dead Guardian Angel does not receive a personal win.

**What does a Pirate win mean?** Two successful duel choices are insufficient if the kills are prevented. The personal objective counts effective plunder kills; achieving it can remain recorded after Pirate dies.

**What survives a restart?** Modern panels can be reopened, persisted duels/trials can resume, and committed night results can finish pending delivery. The bot repairs relevant permissions. Legacy control records have more limited recovery, and Discord sends can repeat if a crash happens between delivery and local acknowledgement.

**Why can an innocent player look suspicious?** Frames and douses affect investigative readings. Tailor changes a death reveal; the engine keeps the underlying role separately. Seer uses alignment buckets rather than exact role identification.

For installation, source layout, testing, and simulator assumptions, see the [README](README.md) and [simulation guide](docs/SIMULATION.md).
