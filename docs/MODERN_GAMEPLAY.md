# Modern gameplay controls and runtime rollout

The implementation keeps discord.py and the existing commands, including the expanded 32-role engine. Standard 64-bit CPython 3.14.8 is the default runtime. On October 6, 2026, the project owner waived the live-server test as an upgrade prerequisite. Python 3.12 and its original environment remain available for rollback.

## Implemented checkpoints

1. **Python compatibility:** `.python-version` selects 3.14.8 and `scripts/run_bot.ps1` launches `.venv314` after checking the interpreter, architecture and standard build. `.venv312-check` validates the same dependency constraints on Python 3.12. The original `.venv` and `constraints-rollback-312.txt` preserve the prior dependency set. Windows CI tests both Python versions and installs with `constraints.txt`.
2. **Shared services:** `gameplay/actions.py` handles command and component submissions with the same role, target, resource, replacement and cooldown checks. State commits and persistence share `Game.state_lock`. Failed writes restore prior state; cancellation finishes pending writes before releasing the lock. Night resolution runs on an isolated model, buffers private feedback, and commits application before delivery.
3. **Trials:** public nominations use a player picker and Abstain; choices can change until closing. Nomination, defense and judgment last 300, 45 and 30 seconds. A unique positive leader starts one of two daily trials. Judgment remains private until closing, then reveals every eligible choice and its frozen weight. A revealed Mayor weighs two. Guilty must exceed Innocent; missing votes are Abstain. Defendant death/departure before application refunds the trial once. Jester targets include eligible Guilty and abstaining voters.
4. **Duels:** private Rock, Paper and Scissors controls accept each participant's first choice. Choices stay hidden until completion. The original 30-second deadline survives recovery; unanswered participants receive random choices. Completion and the result are saved together before resolution becomes available. Completed receipts survive action replacement.
5. **Night panels:** role-specific controls display available abilities, remaining resources, submitted actions and stable target seats. Corpse abilities, two-target abilities, Hypnotist messages and Tailor text use temporary drafts with an explicit Submit. Target and public ballot lists are paged. `/actions` opens a private response; `!actions` delivers privately. Outstanding duels reopen through the same helpers. `/trial` and `!vote` require the configured Game Overseer.

Configured private channels are checked for effective access by ordinary members and broad role permissions. Unsuitable channels fall back to DMs; failed DMs produce a neutral instruction to use `/actions`. Existing action commands retain their configured private-channel restrictions.

## Controls for the five added roles

- **Deputy:** `/actions` and `!actions` reopen a private daytime panel. From Day 2, prepare a target, review the shot, then confirm **Fire**. Selecting a target or cancelling spends nothing. The existing one-bullet, defense, friendly-fire/guilt, and Tribunal restrictions apply. `!shoot` and `/shoot` use the same shot service and refresh the panel. Committed deaths remain recoverable if delivery fails.
- **Serial Killer:** the night panel displays the current **Cautious/Aggressive** mode. The buttons explicitly set that mode, so repeated clicks cannot toggle it accidentally. Mode changes preserve the submitted stab target. `!cautious` retains its toggle syntax.
- **Guardian Angel:** the panel shows the bound player's status, remaining wards, and active night/day protection. **Ward [player]** opens a draft with the bind already selected; **Submit** still saves the action. Living and dead Angels follow the existing eligibility and win rules.
- **Seer:** the two pickers exclude self and revealed Mayors, and filter previously used pairs after a partner is chosen. Private comparison cards and a paged history show the player's chosen pair and the existing result. Hidden transport destinations and Witch-forced targets are not exposed.
- **Psychic:** the night panel explains the passive ability. Vision and interruption messages become private report cards with a paged history, available during day or night. Witch feedback follows the existing engine's recipient rules; history stores only feedback addressed to that owner.

Reports are created on the isolated resolution model and saved with result application before Discord delivery. Histories are scoped to the match and owner, survive restart, and remain readable after death. Older saves start without report history; no past result is reconstructed from hidden game state. Ordinary day panels restore through the same controller as night panels. Concurrent panel delivery is serialized per owner to avoid duplicate messages. Form drafts and shot confirmations remain temporary.

## Recovery and delivery

Version 1 session records contain session/match identity, UTC deadlines, message references, accepted choices, results and application/delivery/progression checkpoints. Startup records retain the original role assignment and completed-player checkpoints. Gameplay remains closed until roster hydration, private-channel ACL repair, and setup complete. A confirmed member departure uses normal death bookkeeping; temporary Discord errors preserve the roster. Drafts and interaction tokens are never stored. Public and ordinary private-message controls register again after recovery. Temporary slash panels reopen through `/actions`.

Expired nominations and judgments close from saved ballots; expired defense starts a fresh 30-second judgment. One controller runs per active session. Reconnects preserve the current Game and its jobs. Invalid old records use the legacy cleanup path. Missing session messages are recreated; unavailable channels cancel unapplied sessions safely and repair voice restrictions. Applied death metadata, graveyard entries, Jester eligibility and Executioner effects share a durable commit with the application checkpoint.

Discord delivery is retried independently of logical application. Trial clocks advance before public fetch/edit operations, and completed trials and night resolutions are retained by session ID so later sessions cannot overwrite pending delivery. Death access cleanup has a separate checkpoint and does not require a public announcement channel. SQLite queue work runs in drained background workers; match-scoped messages are checked against both live state and saved state during restart. Resets wait for startup requests, message delivery, and pending statistics writes before clearing state. Public death notices carry a stable event marker so recovery can find a recently sent notice when interruption happened before its message reference was saved. If all public channels disappear after night application, recovery advances the phase, restores voice access, and retains pending public notices for delivery when a usable channel returns. Undelivered completed duels also reopen privately after action replacement or day transition. There is still an external-service ambiguity if Discord accepts a request but its response is lost; logical deaths, resource use and saved verdicts are not reapplied.

The October 9 recovery update saves successful startup, control, duel, death-notice, and night-feedback delivery progress before graceful shutdown returns. Restart resumes missing deliveries and returns unfinished outbox batch claims to pending. Startup retains its assignment through temporary SQLite locks and retries with the current server cache; unavailable required roles or private channels keep gameplay closed. Interrupted night resolution can retry before application, while an applied result resumes delivery without spending resources again. Saved-game replacement retries brief Windows file locks and preserves the previous file on failure; damaged saves and pending endgame markers remain available for recovery. Simulations use an isolated game model and do not write active-game snapshots. See the [persistence and delivery details](../README.md#persistence-recovery-and-delivery).

## Automated validation

The October 9, 2026 update was validated on CPython 3.12.14 and 3.14.8 with `constraints.txt`; [the published commit passed GitHub Actions](https://github.com/chentaot1/mafiabot/actions/runs/37942475123):

- 699 passed tests and three skipped empty replay collections on each runtime, including the five restored roles, their upgraded controls, and the expanded simulator.
- Standalone smoke checks, command/component parity, every supported corpse role, voting visibility, eligibility, Mayor weighting, ties, abstentions and refunds.
- Persistence failure/cancellation, stale controls, duplicate duel clicks, blocked DMs, unsafe channels, member departures and concurrent submission/resolution.
- 57 audit regression cases cover interrupted startup, temporary membership/read failures, stale grants, full client restart, delayed public I/O, older pending results, safe imports, SQLite contention, connection closure, stale match messages, exhausted corpses, dead wills, and bounded status output.
- Restart at result, application, announcement, permission cleanup and phase progression checkpoints; no repeated logical death or resource expenditure. Concurrent completion delivery, malformed session types, deleted public channels and stale will editors have regression coverage.
- Additional regressions cover delivery during shutdown, reconnect cache replacement, incomplete startup access, corrupt-save quarantine, endgame marker preservation, Windows file locks, SQLite contention, and simulator persistence isolation. The public checkout also passed all 13 configuration checks with blank server defaults.
- 200 seeded engine fuzz iterations plus 47 deterministic scenarios, the 32-role simulator audit and dependency consistency checks on each runtime.

The Python 3.14 run reports upstream discord.py deprecation warnings; these do not fail the checks.

Install or reproduce the checks in a separate environment:

```powershell
py -3.14 -m venv .venv-check
.\.venv-check\Scripts\python.exe -m pip install -c constraints.txt -r requirements.txt -r requirements-dev.txt
.\.venv-check\Scripts\python.exe -m pip check
.\.venv-check\Scripts\python.exe scripts/run_bounded_tests.py --isolated -ra
.\.venv-check\Scripts\python.exe smoke_test.py
.\.venv-check\Scripts\python.exe scripts/sim_test.py --skip-exhaustive --fuzz-iterations 200 --seed 12345
.\.venv-check\Scripts\python.exe scripts/monte_carlo_sim.py --audit
```

## Optional live-server validation — waived as an upgrade prerequisite

The project owner explicitly waived the live-test prerequisite on October 6, 2026. This checklist is retained for optional future testing; no server ID or test token is required to complete the runtime upgrade. No live Discord test has been performed.

For an optional check, use a dedicated test Discord application/server. Put its token in ignored `.env.test`, enable Members and Message Content intents, and invite it with bot/application-command scopes. Create a **Game Overseer** role and a Playing role, record their IDs, and place the test bot above the roles it manages.

The launcher isolates JSON state, SQLite data, the process lock and guild command registration. It does not switch the production runtime or sync global commands:

```powershell
.\.venv314\Scripts\python.exe scripts/run_discord_test.py --check --guild SERVER_ID --overseer-role OVERSEER_ROLE_ID --playing-role PLAYING_ROLE_ID
.\.venv314\Scripts\python.exe scripts/run_discord_test.py --guild SERVER_ID --overseer-role OVERSEER_ROLE_ID --playing-role PLAYING_ROLE_ID
```

An optional real game can use at least five participants, including desktop and mobile clients:

- Check `/actions`, `!actions` and `/trial`; public judgment instructions must disclose no choice before closing. Check editable nominations/judgments and double-weight Mayor voting.
- Check Pirate choice privacy, duplicate clicks, an unanswered participant, and resolution waiting for completion.
- Check ordinary private-channel access, deliberately unsafe configuration, disabled DMs and private slash responses. Verify no public role details or hidden choices.
- Exercise every role panel across appropriate fixtures, especially Retributionist Transporter, Hypnotist, Tailor and a dead Jester. Check replacement and resource use at resolution.
- Restart during nominations, defense, judgment, an active duel, and after verdict application. Check original deadlines, complete results, repaired voice permissions and resumed progression.
- Finish the match, start another, and click old controls; they must reject the earlier identity. Check completed controls and reopened panels on both client types.

Record any future server/client checks here. No live bot has been launched by this upgrade pass.

## Rollout and rollback

Deploy between matches. Stop the old bot before launching the replacement; retain its interpreter/environment, source snapshot and dependency constraints. Preserve a backup of state and SQLite data before switching.

The default launch command is `scripts/run_bot.ps1`, which uses `.venv314\Scripts\python.exe`. Use `scripts/run_bot.ps1 -Check` to verify the runtime without connecting to Discord. If rollback is needed, stop the replacement first and restore the previous source and saved state with the original Python 3.12 environment. Old code cannot resume the new session format faithfully; perform rollback between matches.

Lobby controls, will-editor redesign, leaderboards, broad slash conversion and a Discord Activity remain separate future work.
