# 0007 - secrets in a local file vault

Decision: one file-backed vault replaces the cloud secret stores:
file-vault://<user>/<provider>/<leaf> under the persistence root, 0600
atomic writes, traversal-guarded reads, typed SecretNotFoundError for
missing/legacy refs. The credential-request card flow is unchanged on the
wire.
