# 0004 - TRID3NT everywhere, zero legacy names

Decision (2026-07-21/22): the product name is TRID3NT across all versions.
Zero literal mentions of the pre-rebrand name anywhere - no history-note
exceptions (inclusion only by explicit ask). Layer A (prose/docstrings/UA
strings/fixtures) is done; Layer B renames the identifiers themselves
(packages, env vars via dual-read, logger namespaces, persistence dir with
data migration).
Consequence: env rename uses dual-read (new name wins, old accepted) so
.env files and built worker images migrate without a flag day.
