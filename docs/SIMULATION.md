# Simulation and testing

The project has two different kinds of simulation. They answer different questions and should not be treated as interchangeable evidence.

## Balance modeling

[`scripts/monte_carlo_sim.py`](../scripts/monte_carlo_sim.py) exposes the modular simulator in [`scripts/monte_carlo`](../scripts/monte_carlo). It samples rosters and modeled player decisions, advances day/night cycles, and aggregates faction and personal outcomes. Its bridge executes the production night pipeline with headless members/guilds. Simulation models keep checkpoints in memory without reading or replacing saved bot games, in both single-process and parallel trials. Real games retain their normal saving behavior. The default generator and several win/death helpers are shared with the bot.

It supports:

- Fixed role lists and repeated seeded rollouts.
- Enumeration of five- and six-player role sets, with per-set CSV output.
- Generator-weighted sampling for larger lobbies, including role inclusion/exclusion and faction-count overrides.
- Constraints on investigative/protective roles and adjustable lobby skill assumptions.
- Diagnostic attribution for mislynches, saves, blocks, controls, and other events, plus a single-game trace mode.
- A role-pool audit against the bot's configuration.

For a quick reproducible run:

```powershell
python scripts/monte_carlo_sim.py --audit
python scripts/monte_carlo_sim.py --generator-trials 1000 --player-count 7 --workers 2 --seed 12345 --diagnostics
```

To inspect sampled lobby composition instead of outcomes:

```powershell
python scripts/monte_carlo_sim.py --generator-trials 1000 --player-count 7 --seed 12345 --generator-distribution
```

For a larger enumeration experiment:

```powershell
python scripts/monte_carlo_sim.py --enumerate 5 --n-per 100 --seed 12345 --out-csv scripts/monte_carlo_5p.csv
```

Enumeration can take considerably longer than a single sampled run. Generated reports are ignored by Git. Historical results are not included in this publication.

**Interpretation:** Results depend on modeled decisions, role-selection constraints, and competence assumptions. Competence has targeting, resource-use, and daytime axes; use `--show-competence` to inspect it. Neutral wins can coexist with other outcomes, so every printed probability is not part of one mutually exclusive distribution. Night-engine reuse reduces duplicated resolution logic, but daytime decisions and the headless adapter still have separate assumptions. Use estimates to identify configurations worth testing with people, rather than treating them as measurements of human behavior.

Generator trials support process workers; use an explicit `--workers 2` for a bounded run or `--serial` for one process. Automatic sizing uses CPU count, capped by the number of trials. Fixed-lineup and enumeration modes have their own runtime paths; do not assume every command benefits from the worker option. Large enumerations and systematic interaction sweeps can be expensive even with parallelism.

## Actual engine checks

[`scripts/sim_test.py`](../scripts/sim_test.py) uses test doubles around the actual game code. It contains deterministic interaction scenarios, randomized night actions, and optional exhaustive/systematic coverage. A bounded starting point is:

```powershell
python scripts/sim_test.py --skip-exhaustive --fuzz-iterations 200 --seed 12345
```

[`scripts/ability_self_test.py`](../scripts/ability_self_test.py) checks individual ability scenarios. [`scripts/property_test.py`](../scripts/property_test.py) uses Hypothesis to explore engine invariants. These scripts are additional checks; they are not all collected automatically by pytest.

The fuzzing tools also cover phase changes, state serialization, rehydration, persistence files, and tribunals. [`scripts/bug_finder.py`](../scripts/bug_finder.py) coordinates several lanes; the minimization scripts help turn a failure into a smaller reproducible case. Check each script's `--help` before starting a large run.

## Regression tests and publication limits

`python scripts/run_bounded_tests.py -o addopts= -ra` runs all pytest cases while capping automatic pool sizing to two logical CPUs in the launcher. Tests that explicitly select or mock worker counts still exercise those contracts. Coverage includes database/outbox behavior, restart handling, private controls, concurrent persistence, restored-role integration, engine invariants, and replay fixtures. Fresh installations have no built-in server/player IDs and reject invalid settings before connecting.

Only synthetic fixtures free of the original server's identifiers are included. Missing fixture collections appear as skipped pytest parameter sets. New fuzz failures can contain state or identifiers from their inputs: inspect any generated repro before committing it.

All publication checks ran without a real Discord gateway session. Test doubles cannot verify Discord's current permission hierarchy, notification delivery, or rate-limit behavior. Human players are also a necessary check on confusing rules and social dynamics.
