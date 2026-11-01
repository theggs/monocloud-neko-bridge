# Project instructions

- Read README.md and docs/operations.md before implementation.
- Keep public examples generic. Never commit personal paths, private infrastructure addresses, node records, account credentials, live connection dumps or deployment-specific identifiers.
- Use synthetic fixtures and documentation IP ranges in tests.
- The production integration excludes process identity fields.
- Preserve cumulative connection counters; the official neko-agent computes deltas.
- Prefer explicit upstream failures over fabricated empty snapshots or guessed addresses.
- Keep runtime dependencies in the Python standard library. Do not change existing client routing or credentials during development.
- Run `python3 -B -m unittest discover -s tests -v` after implementation changes.
- Release archives must be checked against upstream checksums; record resolved versions and do not assume `latest` identifies an installed version.
- Keep experiments in ignored work/ and remove them afterward. Local investigation notes belong outside the public repository.
