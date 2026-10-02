# MafiaSalem — Server Member Guide (Roles + FAQ)

This guide is for **server members** playing the bot. It’s intentionally detailed (more than `!myrole`) so new players can learn quickly.

If something here conflicts with what the bot actually does, assume the bot/code is correct and update this doc.

## How this bot works

### Phases

- **Day**: discussion + trials (`!vote`)
- **Night**: submit actions, then the GM resolves (`!resolve`)

### Where to submit night actions (secrecy)

You have two supported “secret” ways to submit night actions:

- **DM the bot (prefix commands)**: `!heal 3`, `!shoot 7`, etc.
- **Use your private player channel** in the server (prefix or slash)

Using night action commands in random public channels is rejected to prevent leaks.

### Slash vs prefix

- **In-server**: most night actions are **hybrid** (`!command` or `/command`)
- **In DMs**: only **prefix** commands exist (no slash)

### Targeting

Most actions target a **slot number** (example: `!heal 3`). Slot numbers are assigned at game start and **do not change** as people die.

### Changing your mind

Submitting again replaces your previous action.

---

## Core commands (players)

### Lobby

- `!join`
- `!players`

### Utility (DM)

- `!myrole`
- `!will`

### Special

- `!reveal` — Mayor (day, server)
- `!haunt [slot]` — Jester (DM only, after lynch)

---

## Night action commands (submit in DM or your private channel)

### Town (night actions)

- `!alert` — Scary Grandma
- `!heal <slot>` — Doctor
- `!investigate <slot>` — Sheriff, Investigator
- `!protect <slot>` — Bodyguard
- `!corpses` — Retributionist (list usable corpses)
- `!reanimate <corpse> <target>` — Retributionist
- `!roleblock <slot>` — Escort
- `!shoot <slot>` — Vigilante
- `!track <slot>` — Tracker
- `!transport <slot1> <slot2>` — Transporter
- `!watch <slot>` — Lookout

### Mafia (night actions)

- `!frame <slot>` — Framer (Nights 1–2)
- `!guard <slot>` — Gatekeeper
- `!hide <slot>` — Gravedigger
- `!hypnotize <slot> <type>` — Hypnotist
- `!investigate <slot>` — Mole
- `!kill <slot>` — Mobster
- `!roleblock <slot>` — Consort
- `!tailor <slot> <fake_role>` — Tailor

### Neutral (night actions)

- `!chaos <slot1> <slot2>` — Chaos
- `!clean` — Arsonist
- `!control <slot1> <slot2>` — Witch
- `!douse <slot>` — Arsonist
- `!ignite` — Arsonist
- `!plunder <slot>` — Pirate
- `!vest` — Survivor

---

## Attack/defense (important interactions)

- **Doctor heal**, **Survivor vest**, and **Scary Grandma alert** can stop normal night kills.
- **Ignite** (Arsonist) is an “unstoppable” mass-kill that burns through defenses.
- **Arsonist** has basic defense against normal night kills.
- **Witch** has a special **Night 1 shield** that blocks one normal kill on Night 1.

## Immunities (important lists)

These are **hard-coded** role immunities in this bot:

- **Roleblock-immune roles**: Scary Grandma, Witch, Consort, Escort, Pirate, Transporter
- **Control-immune roles**: Transporter, Scary Grandma, Witch, Pirate, Chaos

---

## Roles (in-depth)

### Town

#### Retributionist

- **Stats**: Faction: Town | Attack: None | Defense: None
- **Goal**: Town wins
- **Commands**
  - `!corpses` (shows the numbered list of usable corpses)
  - `!reanimate <corpse> <target>` (or `!reanimate <corpse> <t1> <t2>` for Transporter corpse)
- **Uses**: 2 total
- **Key rules**
  - Can’t use **hidden corpses**.
  - Each corpse can only be used once.
  - Only certain **Town** corpses are usable: Doctor, Sheriff, Investigator, Lookout, Tracker, Escort, Transporter, Bodyguard, Vigilante.
  - The `<corpse>` number refers to the list shown by `!corpses` (not the overall graveyard).
- **How to use it well**
  - Save a reanimate for a “swing” night (confirming evil, preventing a kill, or creating an extra death).
  - Using a **Doctor** or **Bodyguard** corpse is usually strongest defensively; using an investigative corpse is strongest for information.

#### Doctor

- **Stats**: Faction: Town | Attack: None | Defense: Basic (self-heal only)
- **Command**: `!heal <slot>`
- **Self-heal**: once per game
- **Can’t heal**: revealed Mayor
- **Feedback**: if your heal mattered, you get ToS-like “Your target was attacked last night!”
- **Extra mechanics**
  - Your heal blocks normal night kills, but **ignite** can still kill through it.
  - If you healed someone during an unstoppable death (ignite), you get explicit feedback that your heal had no effect.
- **Tips**
  - Don’t tunnel on self-heal. A correct heal on a confirmed Town is often game-winning.

#### Sheriff

- **Stats**: Faction: Town | Attack: None | Defense: None
- **Command**: `!investigate <slot>`
- **Result**: innocent vs suspicious
- **Suspicious if**: Mafia, Arsonist, doused, or framed
- **Extra mechanics**
  - **Framed** targets read suspicious.
  - **Doused** targets read suspicious.
- **How to interpret results**
  - “Suspicious” is a strong lead, not an auto-lynch: framing and dousing exist.

#### Investigator

- **Stats**: Faction: Town | Attack: None | Defense: None
- **Command**: `!investigate <slot>`
- **Result**: role bucket list (ToS-style)
- **Framed**: appears as Framer bucket
- **Doused/Arsonist**: appears as Arsonist bucket
- **Extra mechanics**
  - If framed, the apparent result is treated as if the target is **Framer**.
  - If doused (or Arsonist), the apparent result is treated as **Arsonist**.
- **Tips**
  - Buckets are designed for this bot’s role list (smaller lobbies). Combine with claims and vote behavior.

#### Lookout

- **Stats**: Faction: Town | Attack: None | Defense: None
- **Command**: `!watch <slot>`
- **Result**: names of visitors
- **Note**: Gatekeeper is excluded from visit logs
- **Extra mechanics**
  - Lookout uses the bot’s “visit log” — actions like control/transport count as visits in special ways.
  - **Gatekeeper** is intentionally excluded from the visit log, so you won’t see them as a visitor.
- **Tips**
  - Watch likely-kill targets (confirmed Town, revealed Mayor, etc.) to catch attackers/roleblockers.

#### Tracker

- **Stats**: Faction: Town | Attack: None | Defense: None
- **Command**: `!track <slot>`
- **Result**: where your target visited
- **Tips**
  - Tracking a roleblocker/attacker can be as valuable as tracking a quiet player.

#### Vigilante

- **Stats**: Faction: Town | Attack: Basic | Defense: None
- **Command**: `!shoot <slot>`
- **Ammo**: 1 bullet total
- **Guilt**: if you successfully kill Town, you die of guilt the next day
- **Witch**: Witch can force a shot even if you submitted no action (if you still have shots)
- **Extra mechanics**
  - Guilt only applies if your shot actually **killed** a Town member (not if they were healed/defended).
- **How to use it well**
  - Shoot only when you have strong evidence; coordinate with confirms when possible.

#### Bodyguard

- **Stats**: Faction: Town | Attack: Basic | Defense: Basic (self-protect only)
- **Command**: `!protect <slot>`
- **Protect others**: 1 use total
- **Self-protect**: 1 time (vest-style protection)
- **If your protected target is attacked**: you can counter-kill an attacker, and you may die too
- **Pirate**: if Pirate wins a duel on your guarded target, you can still kill Pirate first
- **Extra mechanics**
  - Multiple Bodyguards can protect the same person; only the first counter-kills (others get “someone else protected first”).
- **Tips**
  - Protect publicly valuable targets (revealed Mayor, confirmed investigator, etc.).
  - Self-protect is best used when you are a likely night-kill, not just “because it’s available.”

#### Escort

- **Stats**: Faction: Town | Attack: None | Defense: None
- **Command**: `!roleblock <slot>`
- **Extra mechanics**
  - **You are roleblock-immune** in this bot (Escort is in the roleblock-immunity list).
- **Tips**
  - Blocking suspected attackers is strong, but blocking informational roles can also “test” claims.

#### Scary Grandma

- **Stats**: Faction: Town | Attack: Basic | Defense: Basic (on alert only)
- **Command**: `!alert`
- **Uses**: 2 alerts
- **Effect**: kills visitors; defended against normal kills while on alert
- **Ignite**: ignite burns through alert
- **Extra mechanics**
  - Alert turns visitor info into deaths. If you’re on alert, visitors can die even if you die later to ignite.
- **Tips**
  - Use alert when you expect visits: after a suspicious day, after a public claim, or when you think you’re being targeted.

#### Transporter

- **Stats**: Faction: Town | Attack: None | Defense: None
- **Command**: `!transport <slot1> <slot2>`
- **Effect**: swaps targets for most actions
- **Not redirected**: Pirate actions, vest/clean, transport itself
- **Extra mechanics**
  - Transport applies before most actions and can redirect kills/investigations/blocks.
  - Transport does **not** redirect some self-only actions (vest/clean) and certain special actions (pirate).
- **Tips**
  - Transporting yourself with a high-value Town can “catch” kills and waste Mafia actions.

#### Mayor

- **Stats**: Faction: Town | Attack: None | Defense: None
- **Command**: `!reveal`
- **Voting**: double vote once revealed
- **Important**: cannot be healed after reveal
- **How to reveal**
  - Reveal when it changes the vote math or locks in a lynch. Revealing too early paints a target.

---

### Mafia

#### Mobster

- **Stats**: Faction: Mafia | Attack: Basic | Defense: None
- **Command**: `!kill <slot>`
- **Tips**
  - Coordinate with roleblocks/frames to reduce Town info and protect your kill.

#### Consort

- **Stats**: Faction: Mafia | Attack: None | Defense: None
- **Command**: `!roleblock <slot>`
- **Extra mechanics**
  - **You are roleblock-immune** in this bot (Consort is in the roleblock-immunity list).
- **Tips**
  - Block investigators/lookouts/trackers, or block protection roles before a key kill.

#### Framer

- **Stats**: Faction: Mafia | Attack: None | Defense: None
- **Command**: `!frame <slot>`
- **Only**: Nights 1 and 2
- **What it does**
  - Makes targets look suspicious to Sheriff.
  - Alters Investigator buckets (appears as Framer bucket).
- **Tips**
  - Frame people likely to be investigated early (quiet players, strong Town claims).

#### Gravedigger

- **Stats**: Faction: Mafia | Attack: None | Defense: None
- **Command**: `!hide <slot>`
- **Uses**: 1
- **Effect**: hides role on death; corpse becomes unusable for Retributionist
- **Tips**
  - Hiding a Town power role corpse denies info and denies Retributionist value at the same time.

#### Hypnotist

- **Stats**: Faction: Mafia | Attack: None | Defense: None
- **Command**: `!hypnotize <slot> <type>`
- **Types**: `healed`, `roleblocked`, `transported`, `controlled`, `attacked`
- **How to use it well**
  - Fake “attacked” to bait protection claims.
  - Fake “roleblocked” to justify missing actions.
  - Fake “controlled” to cause mislynches and sow confusion.

#### Mole

- **Stats**: Faction: Mafia | Attack: None | Defense: None
- **Command**: `!investigate <slot>`
- **Uses**: 1
- **Result**: exact role (with douse/arsonist override to “Arsonist”)
- **Tips**
  - Use it to break a pivotal claim (Mayor/Doctor/Transporter) or to find the last Town power.

#### Tailor

- **Stats**: Faction: Mafia | Attack: None | Defense: None
- **Command**: `!tailor <slot> <fake_role>`
- **Uses**: 1
- **What it does**
  - Makes the target’s revealed role appear as your chosen fake role when they die.
- **Tips**
  - Use to “prove” a false narrative (e.g., make a Town corpse look like a neutral/evil).

#### Gatekeeper

- **Stats**: Faction: Mafia | Attack: None | Defense: None
- **Command**: `!guard <slot>`
- **Uses**: 2
- **Effect**: roleblocks non-mafia visitors to the guarded target
- **Stealth**: excluded from Lookout logs
- **Extra mechanics**
  - Gatekeeper blocks **effective** visitors (already-roleblocked players don’t visit).
- **Tips**
  - Guard your kill target to block Doctors/Bodyguards/Lookouts from interfering.

---

### Neutral

#### Chaos

- **Stats**: Faction: Neutral (Chaotic) | Attack: None | Defense: None
- **Command**: `!chaos <slot1> <slot2>`
- **Uses**: 2
- **Goal**: survive to end
- **Important**
  - Your effects are secret/random, so play cautiously.
  - In this bot, Chaos secretly triggers **one non-killing effect** involving your targets, such as:
    - **roleblock**, **transport**, **heal**, **protect**, **investigate**, **watch**, **track**, **frame**, or **hide**
  - If Chaos triggers an info-type effect (like **investigate/watch/track**) or a feedback-type effect (like **heal/protect**), you may receive the usual DMs for that effect.
  - A Chaos use is consumed even if the random effect ends up having no impact (for example, targeting an immune role).

#### Jester

- **Stats**: Faction: Neutral (Evil) | Attack: Unstoppable | Defense: None
- **Goal**: get lynched
- **After lynch**: `!haunt` one guilty/abstain voter (DM)
- **Extra mechanics**
  - `!haunt` with no slot shows the up-to-date eligible list.
  - You can only haunt someone who voted **guilty** or **abstained**.
- **Tips**
  - Don’t overplay. The best Jesters look like “bad Town,” not obvious trolls.

#### Executioner

- **Stats**: Faction: Neutral (Evil) | Attack: None | Defense: None
- **Goal**: get your target lynched
- **If target dies at night**: becomes Jester
- **Tips**
  - Your job is one lynch. Don’t start unnecessary wars after you’ve achieved it.

#### Survivor

- **Stats**: Faction: Neutral (Benign) | Attack: None | Defense: Basic (vested only)
- **Command**: `!vest`
- **Uses**: 2
- **If roleblocked**: vest won’t be used
- **Extra mechanics**
  - Vest is self-only and not redirected by Transporter/Witch.
- **Tips**
  - Vest on nights you expect to be targeted (after claims, late game, or when vote math makes you dangerous).

#### Witch

- **Stats**: Faction: Neutral (Evil) | Attack: None | Defense: Night 1 only
- **Command**: `!control <slot1> <slot2>`
- **Learns role**: of target1
- **Can prevent ignite**: by forcing a douse instead
- **Extra mechanics**
  - If the controlled player submitted no action, Witch can still force certain roles (notably Vigilante) to act.
  - Witch cannot retarget self-only actions like `!vest` or `!clean`.
- **Tips**
  - Use control to “confirm” roles, then force misplays (wasted heals, wrong kills, etc.).

#### Pirate

- **Stats**: Faction: Neutral (Evil) | Attack: Basic | Defense: None
- **Command**: `!plunder <slot>`
- **Goal**: win 2 duels (can still win even if later dead)
- **Roleblocks target**: regardless of duel outcome (unless your target is roleblock-immune)
- **Kills only on win**: plunder becomes a kill if you win the duel
- **Extra mechanics**
  - The duel is a **DM reaction mini-game** (rock/paper/scissors). Both Pirate and target get 30 seconds to choose.
  - If either side times out (or can’t be DMed), the bot picks a random choice for them to keep the duel moving.
  - While any Pirate duel is still running, the GM cannot `!resolve` the night.
  - Pirate is **roleblock-immune** to normal `!roleblock` (Escort/Consort-style).
  - However, a **Gatekeeper** guarding your target can still block your plunder as a “blocked visitor” effect.
  - If you win but a Bodyguard counters you, you can still receive a special “you won, but died first” style message.
- **Tips**
  - Pick targets likely to be unprotected and not on alert.

#### Arsonist

- **Stats**: Faction: Neutral (Killing) | Attack: Unstoppable | Defense: Basic
- **Commands**: `!douse <slot>`, `!ignite`, `!clean`
- **Douse**: marks players; they get “You smell gasoline…”
- **Ignite**: kills all currently doused living players
- **Clean**: removes gasoline from yourself (applies after douses)
- **Extra mechanics**
  - `!clean` is applied after douses, so if you are both doused and cleaned the same night, clean wins.
  - Doused players (and Arsonist) look suspicious to Sheriff and can affect Investigator buckets.
  - If you ignite while you are doused, you burn too.
- **Tips**
  - Don’t ignite too early. The best ignites happen when enough people are doused to end the game or flip the vote math.

---

## FAQ

### Do I need DMs enabled?

No. DMs are optional — you can submit actions in your private channel.

### Why doesn’t `/heal` work in DMs?

Slash commands are guild-scoped. Use prefix commands in DMs.

### My night command didn’t work

Most common reasons:

- it’s not night, or night is resolving
- you’re dead
- you used the command in a public channel (privacy rejection)
- you don’t have the role for that command

### Do night actions “visit” people?

Yes. The bot tracks visits for Lookout/Tracker/Alert-style effects. Some actions are treated specially:

- **Gatekeeper** is excluded from visit logs on purpose (you won’t see them as a visitor).
- **Transport** and **Control** are multi-target and handled specially.

### What happens if I’m roleblocked?

- Your action won’t go through, and you’ll typically get a DM telling you that you were roleblocked.
- Some roles are **roleblock-immune** in this bot (see the “Immunities” section above).

### Can Transporter mess up my action?

Yes. Transport can redirect many targeted actions (kills, heals, investigations, blocks). Some self-only/special actions aren’t redirected (vest/clean, pirate actions, transport itself).

### Can Doctor save someone from ignite?

No. Ignite is an unstoppable mass-kill and burns through defenses like heals/alert.

### When does Vigilante guilt trigger?

Only if your shot actually **kills** a Town member. If your shot fails due to defense/heal, guilt does not trigger from that shot.

### Why did Sheriff say “suspicious” on a Town player?

Because **framing** and **dousing** can both cause Town players to appear suspicious.

### What happens if my Executioner target dies at night?

You become **Jester**.

### Can I still win as Pirate if I die later?

Yes — if you reached 2 duel wins, you can still count as a winner even if you later die.
