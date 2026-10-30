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

# Generated held-out set: sealed

`gen_heldout.yaml` holds 60 scenarios composed by `harness/generator.py` (seeds from 300000)
from held-out mechanisms the agent never sees during development: conn_leak in three
services, invoice_format_change, duplicate_charge, a worker capacity config change,
injected text in invoice data, clock_skew and disk_fill (both new), and invoice_swap in
sync-job (an unseen mechanism/service pair). Every scenario holds at least one of these.

- SHA-256 of gen_heldout.yaml at sealing time: `d1d36c05c144949285c163d1aae313df79d761ebcfef485ce7e5f7f12e7c1c9f`
- Sealed on: 2026-10-30, before the repair agent was committed. An uncommitted agent draft
  existed and was set aside; it was never run on this file (see docs/EVALUATION_LOG.md).
- Rule: same as above. `run_benchmark.py --split gen-heldout` refuses unless `--final` is passed,
  and `python -m harness.generator --split gen-heldout` refuses to overwrite it.
- Only the Null and Oracle reference agents were run on it.
- `tests/test_generator.py` checks both hashes and that the file matches the generator.

    sha256sum scenarios/gen_heldout.yaml        # Linux / Mac
    Get-FileHash scenarios/gen_heldout.yaml     # Windows PowerShell
