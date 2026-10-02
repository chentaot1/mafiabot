# Repro artifacts

This directory contains JSON repro artifacts automatically written by `scripts/sim_test.py` when a
property/invariant check fails.

- Each file includes the **role-set**, **night_actions payload**, and **seed/context** needed to replay/debug.
- These are intentionally machine-readable so we can later add a small `replay_repro.py` runner if desired.
