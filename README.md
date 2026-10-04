# MafiaBot

**A Discord social-deduction game with 27 roles, an ordered night-resolution engine, restart recovery, and a Monte Carlo balance-analysis system.**

MafiaBot hosts a Mafia/Town-of-Salem-inspired ruleset for a Discord server: hidden role assignment, private night actions, daytime nominations and trials, faction and personal win conditions, and persistent player statistics. The repository includes the Discord bot, a separate simulation of repeated games, and tools that exercise the game engine.

**Python · discord.py · asyncio · SQLite · Monte Carlo simulation · Property-based testing**

[Player and role guide](MAFIA_GAME_GUIDE.md) · [Simulation and testing](docs/SIMULATION.md)

## The game

The configured role universe contains **12 Town, 8 Mafia, and 7 neutral roles**. Lobby size changes the available pools and faction counts; weighted selection avoids duplicate roles and limits combinations of killing/disruptive neutrals.

- **Town:** Retributionist, Vigilante, Sheriff, Investigator, Doctor, Escort, Transporter, Mayor, Bodyguard, Lookout, Scary Grandma, Tracker.
- **Mafia:** Mobster, Framer, Gravedigger, Consort, Hypnotist, Mole, Tailor, Gatekeeper.
- **Neutral:** Executioner, Jester, Survivor, Witch, Pirate, Arsonist, Chaos.

Players target stable seat numbers that remain unchanged after deaths. Night abilities use DMs or configured private channels; supported hybrid commands offer slash-command target autocomplete. The bot rejects night-action submissions in unrelated public channels. Players can replace a submitted action before resolution, edit a last will, and consult their role privately.

Daytime play includes nominations, a timed defense, guilty/innocent judgments, double-weight votes for a revealed Mayor, and Jester revenge eligibility. Pirate duels use interactive choices; the night cannot resolve while a duel is unfinished. An overseer controls game start, day/night transitions, resolution, and resets.

The bot creates or reuses game text/voice channels and roles, applies Mafia/graveyard visibility rules, changes voice permissions by phase, and moves dead players to the graveyard voice channel when possible.

## Night-action resolution

Abilities can redirect targets, block actions, change investigative results, or affect attack outcomes. [`engine/night.py`](engine/night.py) resolves these interactions through an ordered pipeline:

1. Apply transports and Witch control/redirection.
2. Build visit records and resolve roleblocking and Gatekeeper guards, including chains, immunities, and repeated-state detection.
3. Inject a Chaos effect, then recompute visits and blocking so downstream abilities see the changed actions.
4. Collect healing/protection and resolve investigations against the effective visits and role states.
5. Resolve attacks, defenses, counterattacks, deaths, and private feedback.

The command layer expands Retributionist corpse abilities into engine actions. Other mechanics include falsified investigative/death information, limited-use resources, Bodyguard counterkills, Vigilante guilt, Arsonist dousing and ignition, and Executioner-to-Jester conversion. These mechanics affect visits, outcomes, feedback, and later nights.

Faction victories and personal victories are tracked separately: a Pirate can meet a personal objective, for example, while the game continues toward another faction's outcome. The [game guide](MAFIA_GAME_GUIDE.md) documents the particular rules used here.

## Persistence and recovery

- **Game snapshots:** JSON state is written through a temporary file and replacement. Restart handling rebuilds game/member references and attempts to repair player roles and voice permissions.
- **Tribunal recovery:** persisted subphases and deadlines allow a defense to resume when enough time remains; invalid or overdue trials are aborted and permissions repaired.
- **Duplicate-action guards:** phase changes, verdicts, ability-use accounting, and endgame handling include guards against repeated execution. The SQLite stats path uses a durable game key to skip repeat match/stat submissions.
- **Player history:** SQLite stores matches, participant roles, faction outcomes, personal wins, aggregate statistics, and per-role results. Players can view stats and interactive leaderboards.
- **Queued role messages:** a SQLite DM outbox provides deduplication keys, retry scheduling, and recovery of stale in-flight queue entries. Other game feedback also uses direct sends; this is not a guarantee of exactly-once Discord delivery.
- **Connection handling:** the bot includes a single-instance lock, reconnect handling, gateway watchdog, and command-sync controls.

## Monte Carlo balance analysis

[`scripts/monte_carlo_sim.py`](scripts/monte_carlo_sim.py) simulates games through day/night cycles using modeled player decisions and aggregates faction and neutral outcomes. It supports:

- Reproducible seeded rollouts for a fixed role list.
- Enumeration of five- and six-player compositions, with per-composition outcome rates and estimated generator frequencies written to CSV.
- Generator-weighted sampling for larger lobbies and a separate composition-distribution mode.
- Role inclusion/exclusion, faction-count overrides, and investigative/protective-role constraints for comparing proposed configurations.
- Adjustable player-competence assumptions that mix heuristic and random decisions.
- Diagnostic attribution for events such as mislynches, heals, blocks, and controls, plus individual-game traces and an audit against the bot's role pool.

This is a separate decision model, so its estimates describe those assumptions. It helps investigate role combinations; it does not establish human win rates or prove perfect agreement with the live engine. Neutral successes can overlap with faction outcomes.

```powershell
python scripts/monte_carlo_sim.py --audit
python scripts/monte_carlo_sim.py --generator-trials 1000 --player-count 7 --seed 12345 --diagnostics
```

## Game-engine testing

The repository also tests the game engine with Discord test doubles:

- Deterministic ability/interaction scenarios, randomized night actions, and optional scenario enumeration in [`sim_test.py`](scripts/sim_test.py).
- Hypothesis-generated role/action inputs, including malformed targets, in [`property_test.py`](scripts/property_test.py).
- Fuzzing tools for phase transitions, state serialization, member rehydration, persistence files, and tribunals.
- Failure capture, replay suites, and minimizers that reduce a generated failure into a smaller reproducible case, coordinated by [`bug_finder.py`](scripts/bug_finder.py).
- Regression coverage for the database/outbox, restart behavior, private-channel guards, and specific role-interaction bugs, alongside the standalone smoke suite.

The publication checks passed **49 pytest tests, with four skipped empty replay collections**, plus the standalone smoke suite, a bounded engine run, and the simulator role audit. [Windows/Python 3.12 CI](.github/workflows/tests.yml) runs these bounded checks on pushes and pull requests. Larger fuzzing, enumeration, and property-test runs are available separately.

These checks use test doubles. A real server still needs permission, message-delivery, reconnect, and playtesting checks. See the [simulation guide](docs/SIMULATION.md) for commands and interpretation.

## Source layout

- [`bot.py`](bot.py): Discord commands, lobby generation, trials, duels, startup, and recovery.
- [`game.py`](game.py): game state, phase transitions, infrastructure, deaths, win conditions, and stats integration.
- [`engine/night.py`](engine/night.py): ordered night-action resolution.
- [`config.py`](config.py), [`roles.py`](roles.py), [`checks.py`](checks.py): server configuration, role definitions, and action-channel checks.
- [`database.py`](database.py), [`persistence.py`](persistence.py): SQLite history/outbox and JSON snapshots.
- [`scripts`](scripts), [`tests`](tests), [`invariants.py`](invariants.py), [`smoke_test.py`](smoke_test.py): simulation, fuzzing, replay, invariants, and regression tooling.

## Run on your server

Use **Python 3.12**, the version used by CI. This bot is configured for one allowed guild per installation.

```powershell
git clone https://github.com/chentaot1/mafiabot.git
cd mafiabot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill in `.env` with your own `DISCORD_TOKEN`, `ALLOWED_GUILD_ID`, `PLAYING_ROLE_ID`, and `GAME_OVERSEER_ROLE_ID`. `DISCORD_BOT_TOKEN` is also accepted as a token alias. `GAME_CATEGORY_ID=0` lets the bot find/create its game category; `PLAYER_PRIVATE_CHANNEL_IDS={}` uses DMs unless you supply your own mapping.

Enable the **Server Members** and **Message Content** intents for the Discord application. Invite it with bot and application-command scopes, allow it to manage the game channels/roles and move members, and put its role above the roles it needs to manage. Give it the text/reaction and voice access required by the game channels.

```powershell
python bot.py
```

Players use `!join`; the configured overseer starts a game with `!startgame` and advances it with `!night`, `!resolve`, and `!day`. Full commands and role instructions are in the [player guide](MAFIA_GAME_GUIDE.md).

To run the regression checks:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -ra
python smoke_test.py
python scripts/sim_test.py --skip-exhaustive --fuzz-iterations 200 --seed 12345
```

Tokens, live server state, databases, logs, and generated simulation reports are excluded from Git. The retained replay fixtures are synthetic; inspect newly generated failures before publishing them.

## License

Copyright (c) 2026 chentaot1.

MafiaBot's original source code and documentation are licensed under the [GNU Affero General Public License, version 3](LICENSE) (`AGPL-3.0-only`). You may redistribute and modify them under that license. The software is provided without warranty, including any implied warranty of merchantability or fitness for a particular purpose.

Commercial use is permitted under the license. If you run a modified version that users interact with over a network, you must prominently offer those users access to the corresponding source code of that version, as required by section 13. For a Discord deployment, provide an accessible source link or command that points to the source of the version you actually run, including your modifications.

Third-party dependencies and any third-party material retain their own licenses and terms; this license grants no rights to material owned by others.
