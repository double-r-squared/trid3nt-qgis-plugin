# TELEMAC family migration - deck parity, hashed

The four AOI templates were migrated onto the workflow skeleton on 2026-08-25
(`df69486c`, `f44588ac`, `fbd013aa`, `bd0b84cd`). Each commit message and the
close-out table in `telemac-family-migration-inventory.md` assert the authored
deck came out BYTE-IDENTICAL across the migration. Nothing persisted the proof:
the claim rested on a comparison made once, in a session, and thrown away.

This document is that proof, re-produced from the object store. Every hash below
was computed by reading the artifact back out of `trid3nt-runs` (decks) and
`trid3nt-cache` (staged worker manifests) and running sha256 over the bytes. No
deck was re-authored to make this table.

## How the pre-migration run is identified

The pairs are not asserted from memory. Two independent facts pin them:

1. The skeleton's persist hook is what writes `metrics.json` and
   `chart_spec.json` under a run prefix. The pre-migration bespoke composers
   wrote neither. Exactly one run per template, immediately preceding that
   template's migrated runs, carries the deck and the solver artifacts but NO
   `metrics.json` and NO `chart_spec.json`.
2. Each staged worker manifest lands in `trid3nt-cache` under
   `<solver>/<run-tag>/manifest.json`. The cache holds exactly TWO manifests per
   template - one for the run identified in (1) and one for the migrated canary
   of record - and no others. The store itself preserves the pair.

The "new" run in every pair is the run named by that template's committed
canary evidence JSON under `docs/proof/templates/`.

## coastal_tidal_surge

- old (pre-migration): `01M0VTGQPNSTVDEHSNH231XCV2` - 2026-08-25T06:43:04Z
- new (migrated canary of record): `01M0VVBYVXTNBN1YFC53NVR5HY` - 2026-08-25T06:57:58Z
- both: 2397 nodes / 4600 elements at 250 m, ocean_edge E, peak_wl 5.3869 m

| artifact | bytes | old sha256 | new sha256 | verdict |
|---|---|---|---|---|
| `t2d_coastal.cas` | 1872 | `9bce57b5cc214a6c0f29a4c8f2f99456ba7921c0d97180099b3c950a6f6f2c60` | `9bce57b5cc214a6c0f29a4c8f2f99456ba7921c0d97180099b3c950a6f6f2c60` | BYTE-IDENTICAL |
| `bc_coastal.cli` | 16563 | `9a2129a639870a64e8876ac2e382c0c70bcecd3c68a793ca48b33b52491333b6` | `9a2129a639870a64e8876ac2e382c0c70bcecd3c68a793ca48b33b52491333b6` | BYTE-IDENTICAL |
| `geo_coastal.slf` | 93852 | `c900bbe44d8f25b550bd0ed4f6393d17a13b60b36e51871641cbfb54fd95b465` | `c900bbe44d8f25b550bd0ed4f6393d17a13b60b36e51871641cbfb54fd95b465` | BYTE-IDENTICAL |
| staged `manifest.json` | 35529 | `c90034edbaec51adef8333531017f06ae96f20e952a6c0282cbd24971558ee24` | `31e0137b27ea754ab51abef8e2ba4c52deb29138dcb0088ab7dbbaac1dae32ac` | IDENTICAL BAR THE RUN TAG |

Manifest prefixes: `coastal/01M0VTGQP8XFDB754YRTYRPSRX/` and
`coastal/01M0VVBYV6FF3X1HM4PRJ13835/`.

## tomawac_wave_field

- old: `01M0VVT3R42E247RFNFCJVTDTQ` - 2026-08-25T07:05:40Z
- new: `01M0VWM75FQS0ZZE7G6RVD008N` - 2026-08-25T07:19:55Z
- both: 494 nodes / 900 elements at 3000 m, hs_max 0.7164 m

| artifact | bytes | old sha256 | new sha256 | verdict |
|---|---|---|---|---|
| `tom_wave.cas` | 1117 | `200a95683a1dc52db8d051db17db97df7fa4ee7853f640a03fa491f1893895cb` | `200a95683a1dc52db8d051db17db97df7fa4ee7853f640a03fa491f1893895cb` | BYTE-IDENTICAL |
| `bc_wave.cli` | 6622 | `f984d5a7b9c8c6e9aacf5ec66a0da10cca17845af84fbf815b44ccbe0c17cbba` | `f984d5a7b9c8c6e9aacf5ec66a0da10cca17845af84fbf815b44ccbe0c17cbba` | BYTE-IDENTICAL |
| `geo_wave.slf` | 19004 | `17efbf1c6478b51d7917c283322cd04ea09d574b34a80478e52639e47dcc7eee` | `17efbf1c6478b51d7917c283322cd04ea09d574b34a80478e52639e47dcc7eee` | BYTE-IDENTICAL |
| staged `manifest.json` | 692 | `47b400bb30830688fb12a9772e14cfe742ff64c42cb23800bd707bfbbe3fe4a7` | `dcf5122cfdeb97a0a15f4a385b0fcfc6e0e3ab8c945c20ad8212f47599ccc817` | IDENTICAL BAR THE RUN TAG |

Manifest prefixes: `tomawac/01M0VVT3QTYPY5NQGB0BQQ9AMM/` and
`tomawac/01M0VWM73GQ5GN7X21ZA1BV4KG/`.

## artemis_harbor_agitation

- old: `01M0VWZSRQ68WXBVY85QKY0EPP` - 2026-08-25T07:26:13Z
- new: `01M0VXF9NXPKSQ0CP5PAEF43YX` - 2026-08-25T07:34:40Z
- both: 2672 nodes / 5052 elements at 30 m, kd_max 3.947

| artifact | bytes | old sha256 | new sha256 | verdict |
|---|---|---|---|---|
| `art_agit.cas` | 466 | `6811ce40b125383a51546eebdbb58ea6e5395a9727a6fa330e09c3d511a7d04f` | `6811ce40b125383a51546eebdbb58ea6e5395a9727a6fa330e09c3d511a7d04f` | BYTE-IDENTICAL |
| `bc_agit.cli` | 22910 | `5a1b60f337dfe57e66a66f6d188a65858d45aed85d1c216e6a1eed535d8008ef` | `5a1b60f337dfe57e66a66f6d188a65858d45aed85d1c216e6a1eed535d8008ef` | BYTE-IDENTICAL |
| `geo_agit.slf` | 103676 | `c799989b05761522451f97082a33d5a3ad7a116bbab6f7ea943f07dd913ac1b1` | `c799989b05761522451f97082a33d5a3ad7a116bbab6f7ea943f07dd913ac1b1` | BYTE-IDENTICAL |
| staged `manifest.json` | 12368 | `4ce4a41775580254b89d8ad51c955ac09f94e54ed0002a119e655c7e6fcd8ca7` | `87f6a18b5bca0ad79531cda20aa0b91701089ef3bfe36faa83590bc3dc369918` | IDENTICAL BAR THE RUN TAG |

Manifest prefixes: `artemis/01M0VWZSREEV5MHPT33Q7K1S12/` and
`artemis/01M0VXF9N9C2WTVRJ1ZCYKAG18/`.

The intervening run `01M0VXB4R6AYBQA6MZ30YG3V6P` (07:32:53Z) is the attempt the
migration commit records as differing: Overpass 504'd on all three mirrors and
the run degraded to the labeled schematic barrier (kd_max 2.837, 2728 nodes). Its
deck hashes differ from BOTH sides of the pair above, which is the fallback norm
behaving correctly on a different input rather than a deck regression. It is
listed here so a future audit that finds three artemis runs in the window does
not read the odd one out as a parity failure.

## telemac3d_stratified_flow

- old: `01M0VY1YPR8K0GGN88KX7463HV` - 2026-08-25T07:44:59Z
- new: `01M0VYDEPH9J1E20NS18K0830G` - 2026-08-25T07:51:13Z
- both: 494 nodes / 900 elements / 13 sigma planes at 3000 m

TELEMAC-3D persists NO `.cas` under its run prefix: the steering file is authored
inside the worker from the staged manifest, so the manifest IS the deck for this
template. That is why the migration close-out row says "manifest BYTE-IDENTICAL"
where the other three say "deck".

| artifact | bytes | old sha256 | new sha256 | verdict |
|---|---|---|---|---|
| `bc_t3d.cli` | 6622 | `f984d5a7b9c8c6e9aacf5ec66a0da10cca17845af84fbf815b44ccbe0c17cbba` | `f984d5a7b9c8c6e9aacf5ec66a0da10cca17845af84fbf815b44ccbe0c17cbba` | BYTE-IDENTICAL |
| `geo_t3d.slf` | 19004 | `9916f6a750fd34234457f189d1a4976331a49227ec9be62d8545d42c5fa6df0e` | `9916f6a750fd34234457f189d1a4976331a49227ec9be62d8545d42c5fa6df0e` | BYTE-IDENTICAL |
| staged `manifest.json` | 691 | `de068acd33f9d665be4fdbcde2ff5b9b14643f9626557aa3aa2271abc4aff62a` | `f29ca10dbd4ab222e59dd82fa6bd8b305d584e9384e1ed508054d69c102cf14c` | IDENTICAL BAR THE RUN TAG |

Manifest prefixes: `telemac3d/01M0VY1YPFGM41WGQKWQ9VJY3W/` and
`telemac3d/01M0VYDEMJZ5G4K8VHDJA1XB65/`.

## What "identical bar the run tag" means, exactly

Not a hand-wave. A unified diff of the two manifests, in all four cases, is TWO
lines long and both are the same key:

```
-  "run_id": "<old run tag>",
+  "run_id": "<new run tag>",
```

Every other byte - the physics slot, the bathymetry source, the resolution, the
bbox, the declared inputs, the declared outputs - matches. With the run tag
normalised out, the manifests hash equal:

| template | normalised sha256 (both sides) |
|---|---|
| coastal_tidal_surge | `f33922482983026aa7411622495fcfb6dc144bd6900219c41bd94123cee4483f` |
| tomawac_wave_field | `881b2dad12b152189cc6638a23360435d57f8fa4df75ffd5368e17dba4e3c6d5` |
| artemis_harbor_agitation | `bf4919ccd41986148642443c51a1c2e9ff81b71f453738967fc9a6fbca63ba58` |
| telemac3d_stratified_flow | `082a1563410bf9a6a44e27f6ea9830d6da76d849de95ba7d08ecf881a229d711` |

## Verdict

All four claims hold. Every `.cas`, every `.cli` and every geometry SELAFIN is
byte-identical across the migration boundary - no normalisation, no tolerance,
equal hashes on the raw bytes. Every staged worker manifest differs in exactly
one field, the run tag, which is minted per run and cannot be equal by
construction.

No pair failed. Nothing was buried.

## Reproducing this

```
set -a; source .env.local; set +a
```

then read each key out of `$TRID3NT_RUNS_BUCKET` / `$TRID3NT_CACHE_BUCKET` with
boto3 against `AWS_ENDPOINT_URL` and sha256 the bytes. The run prefixes above are
the complete input list. Never use ambient AWS credentials for this - the repo's
AWS account is decommissioned and `scripts/_env_guard.py` exists to refuse that
fallback.
