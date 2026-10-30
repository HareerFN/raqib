# Held-out set: sealed

`heldout.yaml` contains failure families the agent never sees during development.

- SHA-256 of heldout.yaml at sealing time: `8b378896412ab004727aea45950e07b5854266ef1627ae28974e4ba1a4fa57a8`
- Sealed on: 2026-10-30, before any repair agent code was written.
- Rule: the repair agent is never run on this file until final evaluation
  (`run_benchmark.py` refuses unless `--final` is passed).
- Only the Null and Oracle reference agents were run on it, to prove that
  failures do not heal on their own and that every fixable case can be fixed.

Anyone can verify the file was not changed after sealing:

    sha256sum scenarios/heldout.yaml        # Linux / Mac
    Get-FileHash scenarios/heldout.yaml     # Windows PowerShell

The Git history shows this file was committed before the agent existed.
