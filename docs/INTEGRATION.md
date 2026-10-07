# Recovered engine and modern controls

This version combines the 32-role MafiaSalem engine with the later MafiaBot controls. It is a source integration, not a merge of the old Git histories.

## Source provenance

The recovered engine, role generator, combat/win helpers, and modular Monte Carlo system came from local historical MafiaSalem commit `e37411614249bea640fdc169b34aef6d1a34ed65`. Recovery also used the Deputy helper and simulator bridge from the following `a86cdb4` snapshot. Four files missing from the historical checkout were reconstructed during recovery: `bot.py`, `night_action_guards.py`, `stats_mirror_repair.py`, and `persist_validation.py`. The combined version uses the later command entrypoint in place of the reconstructed `bot.py`; the other three reconstructed helpers are retained and covered by regressions. They should not be described as byte-for-byte historical originals.

The modern command layer, Components V2 controls, persistent duels/trials, guarded state commits, asynchronous persistence fixes, and OS-owned instance lock came from the local `Mafiabot_v2` snapshot reviewed on October 6, 2026. Existing public tests and synthetic replay fixtures were retained.

## Integration choices

- The recovered 32-role game model and night pipeline remain the rule engine. Psychic, Deputy, Seer, Guardian Angel, and Serial Killer are connected to role assignment, commands, and modern private controls where applicable. Psychic is passive; Deputy uses the daytime shooting command.
- Commands and components share action validation. Resolution calculates on a copied model and buffers private messages before applying a durable result. Transport state is copied too, so a failed calculation cannot mutate the live model or leak buffered results.
- The later Transporter-corpse workflow is retained and implemented in the shared corpse-expansion code used by live resolution and the simulator. This is an intentional extension to the recovered corpse allow-list.
- Default live and simulated roster generation share `game_roles.draw_roles_for_startgame`: small-lobby TI/TP/Random Town manifests, weighted larger pools, and selective uniqueness rules. Lobby composition differs from the earlier 27-role public version.
- Modern trial/duel records resume through the controller. Historical snapshots without those records receive narrower compatibility handling. The recovered night checkpoint helpers remain available to finish historical engine states.
- Deputy has one bullet per player for the game, matching the recovered per-player accounting. Guardian Angel can ward after death but requires survival for a personal win. Player-facing descriptions were corrected to match these rules.
- A pending endgame statistics marker blocks starting a new match until its commit succeeds. Tokens, live databases, player state, and historical Git objects are not included in this publication.

## Validation

The initial integration was checked offline on Windows with Python 3.12, including standalone smoke checks, 47 deterministic engine scenarios with 200 seeded fuzz iterations, the 32-role simulator audit, 50 generated matches with two workers, and 10 fixed-lineup matches containing all five restored roles. Subsequent gameplay and compatibility checks passed 394 pytest cases (three empty replay collections skipped), the 47 scenarios, 200 seeded fuzz iterations and the role audit on both Python 3.12.14 and 3.14.8. This includes the new day controls, explicit mode selection, ward drafts, report cards and durable private histories. Standard 64-bit CPython 3.14.8 is now the default; Python 3.12 remains the rollback runtime.

Tests cover private command/component behavior, restart receipts, persistence cancellation/FIFO ordering, role assignment, passive visions, Guardian Angel nomination protection, Deputy deaths, Seer results, Serial Killer mode changes, Transporter corpse redirection, and rollback of buffered resolution. Static smoke checks were adapted to follow shared modules; behavioral expectations were updated where the recovered rules intentionally differ from the 27-role version.

No real Discord gateway session was opened for these checks. The project owner waived the live-server test as an upgrade prerequisite on October 6, 2026; it remains an optional check of permissions, delivery and actual gameplay. Simulator runs here check integration and execution; their small sample sizes do not establish game balance. Modeled daytime behavior and player competence remain assumptions even though night resolution uses the production engine.
