# 0002 - one daemon, one user, one process

Decision: keep the server a single monolithic process (WS + tool dispatch +
turn loop + persistence + HTTP catalog). Microservice splits solve
multi-tenancy problems we do not have; revisit only at >1 concurrent user.
