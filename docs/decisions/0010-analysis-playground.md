# 0010 - analysis is composed, not enumerated

Decision: atomic tools are DATA fetchers and irreducible primitives only.
Analyses/impact studies are composed by the model in the sandboxed python
playground (code_exec) - flexible and auditable - rather than shipping a
rigid tool per analysis. Hand-rolled analytical lookup tools fold into a
spatial-SQL surface (DuckDB) where a one-liner replaces bespoke code.
