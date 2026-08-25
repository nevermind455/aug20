# A.5 archive marker

This project originated from the A.5 multi-trade snapshot. The old launch and
credential instructions are intentionally retired because they bypassed the
current paper-default runner, durable accounting, process lock, and safety
gates.

Use `README.md` for the current commands. `strategy.py` remains byte-identical
to the supplied strategy (`SHA-256 c58e9e3e3a3c72f8aa30377d66b37b8ba5c7b281be0d012ddebd176950955c2e`).
Multiple trades per round are still allowed, subject to the configured round
exposure cap and duplicate/ambiguous-submission guards.
