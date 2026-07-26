# Malpasset dam-break (1959) - TELEMAC-2D validation case: sources and provenance

Reyran valley, Var, France. Arch dam failed 2 December 1959, 21:14; ~421-433 fatalities.
This directory holds the official TELEMAC-2D `malpasset` validation case plus a transcribed
observation set (police high-water marks, transformer arrival times, physical-model gauges).

Every number below carries its source. A downstream auditor should re-check against the cited
sources. Where sources disagree, both values are recorded with the choice justified.

--------------------------------------------------------------------------------
## 1. CASE FILES (mesh / boundary / steering)

Downloaded 2026-07-26 from the GitHub mirror of the OpenTELEMAC examples tree:
  repo:   https://github.com/ogoe/OpenTelemac  (branch `master`)
  path:   examples/telemac2d/malpasset/
  raw:    https://raw.githubusercontent.com/ogoe/OpenTelemac/master/examples/telemac2d/malpasset/<file>

This mirror carries a modern TELEMAC (references the NERD scheme (14), ERIA scheme (15) and the
treatment of negative depths introduced in v7.0; steering-file version banners run through v7.2 /
v6.2). The canonical upstream is gitlab.pam-retd.fr/otm/telemac-mascaret (opentelemac.org); the
ogoe GitHub mirror was used because it is directly fetchable and byte-serves the same example.

File blob SHAs (git, from the GitHub contents API, for integrity):
  geo_malpasset-small.slf  a15e48bfe89384b49d06a4328d8a1fd2d857786c  (962621 B)
  geo_malpasset-small.cli  (77760 B)
  geo_malpasset-large.slf  (2097565 B)
  geo_malpasset-large.cli  (174960 B)
  f2d_malpasset-small.slf  a15e... reference RESULT file (1016677 B)

Files acquired here:
  geo_malpasset-small.slf   SELAFIN geometry, SMALLEST canonical mesh variant (PRIMARY)
  geo_malpasset-small.cli   boundary conditions for the small mesh
  geo_malpasset-large.slf   SELAFIN geometry, large refined mesh (reference)
  geo_malpasset-large.cli   boundary conditions for the large mesh
  t2d_malpasset-small_charac.cas  steering, small mesh, historical method-of-characteristics advection
  t2d_malpasset-small_pos.cas     steering, small mesh, NERD (14) negative-depth scheme (validation default)
  t2d_malpasset-small_ERIA.cas    steering, small mesh, ERIA (15) scheme
  t2d_malpasset-small_cin.cas      steering, small mesh, 1st-order kinetic FV scheme
  t2d_malpasset-small_prim.cas     steering, small mesh, coupled primitive equations
  t2d_malpasset-large.cas          steering, large mesh, NERD (14)
  f2d_malpasset-small.slf          reference RESULT (TELEMAC output) for the small mesh, for solution cross-check
  malpasset.xml                    validation harness config
  doc/malpasset.tex                bundled case documentation

### Mesh variant identification (verified locally by parsing the SELAFIN headers)
  geo_malpasset-small.slf : 26,000 triangular elements / 13,541 nodes / 3 nodes-per-elem
      title "TELEMAC 2D : RUPTURE DE BARRAGE SUR FOND SEC"
      -> this is the classic "regular" mesh used in the Hervouet & Petitjean (1999) validation.
         It IS the smallest full case mesh shipped (there is no coarser canonical variant;
         the "small"/"large" pair is regular(13541 nodes) vs refined(53081 nodes)).
  geo_malpasset-large.slf : 104,000 elements / 53,081 nodes.
The bundled doc (doc/malpasset.tex) states the same counts:
  "Regular mesh: 26,000 triangular elements / 13,541 nodes ... Large mesh: 104,000 triangular
   elements / 53,081 nodes."

--------------------------------------------------------------------------------
## 2. FRICTION BASELINE (as shipped)

From t2d_malpasset-small_charac.cas and t2d_malpasset-large.cas (bundled steering files):
  LAW OF BOTTOM FRICTION = 3        -> Strickler law
  FRICTION COEFFICIENT   = 30.      -> Strickler K = 30 m^(1/3)/s, uniform
  VELOCITY DIFFUSIVITY   = 1.       -> constant horizontal viscosity 1 m^2/s
  Channel banks: solid slip (no roughness); bottom: Strickler K=30 (doc/malpasset.tex).

So the SHIPPED baseline is a uniform Strickler K = 30 m^(1/3)/s (Manning n = 1/K = 0.0333).

--------------------------------------------------------------------------------
## 3. PUBLISHED CALIBRATION / FRICTION BAND

- TELEMAC bundled case: uniform Strickler K = 30 (see section 2).
- Hervouet, J.-M. (2000) "A high resolution 2-D dam-break model using parallelization",
  Hydrological Processes 14:2211-2230 -> the value adopted in later reproductions is
  Manning n = 0.033 (equivalently Strickler ~30.3). FullSWOF (Delestre et al., arXiv:1401.4125,
  section 6.3.3) states: "We consider the Manning law with n = 0.033 m^-1/3 s, as advised in
  Hervouet (2000)." ANUGA malpasset study likewise uses Manning 0.033.
- The commonly reported Strickler band across published Malpasset validations is ~30-40
  (K=30 in valley/plain up to ~40 in the main channel in some calibrations). The shipped case
  uses the single uniform K=30 value; higher channel values (up to ~40) appear in zone-varying
  calibrations in the literature.

--------------------------------------------------------------------------------
## 4. OBSERVED DATA (see observations.json for the machine-readable transcription)

PRIMARY NUMERIC SOURCE for the transcribed tables:
  Biscarini, C.; Di Francesco, S.; Ridolfi, E.; Manciola, P. (2016).
  "On the Simulation of Floods in a Narrow Bending Valley: The Malpasset Dam Break Case Study."
  Water 8(11):545. https://doi.org/10.3390/w8110545  (open access; MDPI).
  Fetched via the Internet Archive Wayback Machine snapshot dated 2026-01-13 of the MDPI HTML
  (direct MDPI access was blocked to automated clients; the archived HTML tables were parsed).
  The paper states: "The authors are grateful to Jean-Michel Hervouet for kindly providing the
  observed and laboratory data."

PRIMARY ORIGIN of the data (as cited by Biscarini et al. 2016):
  - Goutal, N. (1999) "The Malpasset dam failure - an overview and test case definition",
    Proc. 4th CADAM Meeting, Zaragoza, Spain, 18-19 Nov 1999. [ref 46 in the paper]
  - Morris, M.W. (2000) "CADAM: Concerted Action on Dam-break Modelling", Report SR 571,
    HR Wallingford. [ref 8 in the paper]
  - Hervouet, J.-M.; Petitjean, A. (1999) "Malpasset dam-break revisited with two-dimensional
    computations", J. Hydraulic Research 37(6):777-788. [ref 18]  (paywalled; not directly read)
  - Alcrudo, F.; Gil, E. (1999) "The Malpasset dam-break case study", 4th CADAM Workshop,
    Zaragoza, pp. 95-109. [ref 9]
NOTE ON CHAIN: the paywalled/offline primaries (JHR 1999, CADAM report SR571, CADAM proceedings)
could not be opened directly in this offline-first environment; the numbers were transcribed from
the OPEN reproduction (MDPI 2016) which explicitly attributes them to the CADAM/Hervouet sources.
This transcription chain is disclosed so the auditor can weight it.

### (a) 17 police survey points P1-P17  [Biscarini et al. 2016, Table 3]
Surveyed maximum water-surface elevation (m), local police high-water marks. x,y in the local
map frame (metres). Verbatim (thousands separators removed; e.g. "10,957.2" -> 10957.2):

  id   x_m       y_m      bank    ws_obs_m
  P1   4913.1    4244.0   Right   79.15
  P2   5159.7    4369.6   Left    87.20
  P3   5790.6    4177.7   Right   54.90
  P4   5886.5    4503.9   Left    64.70
  P5   6763.0    3429.6   Right   51.10
  P6   6929.9    3591.8   Left    43.75
  P7   7326.0    2948.7   Right   44.35
  P8   7451.0    3232.1   Left    38.60
  P9   8735.9    3264.6   Right   31.90
  P10  8628.6    3604.6   Left    40.75
  P11  9761.1    3480.3   Left    24.15
  P12  9832.9    2414.7   Right   24.90
  P13  10957.2   2651.9   Right   17.25
  P14  11115.7   3800.7   Left    20.70
  P15  11689.0   2592.3   Right   18.60
  P16  11626.0   3406.8   Left    17.25
  P17  12333.7   2269.7   Right   14.00

### (b) 3 electrical transformers A,B,C  [Biscarini et al. 2016, Table 2]
Wave arrival time inferred from transformer electrical shutdown after dam failure.

  id   x_m       y_m      at_obs_s
  A    5500      4400     100
  B    11900     3250     1240      (TUFLOW 2019 reports 1204 s for B; see discrepancy note)
  C    13000     2700     1420

  Discrepancy: TUFLOW "Malpasset Dambreak Benchmarking" (tuflow.com/insights/2019_03-malpasset)
  lists recorded transformer times A=100, B=1204, C=1420 s. MDPI/CADAM give B=1240 s. The 1240 s
  value is adopted as primary (consistent with the CADAM/Hervouet dataset); 1204 s is retained as
  the TUFLOW variant in observations.json (`at_obs_s_alt_tuflow`).

### (c) 9 physical-model gauges (points 6-14; labelled G6-G14)  [Biscarini et al. 2016, Table 4]
1:400 scale model, Laboratoire National d'Hydraulique (EDF-LNH), 1964. Values at prototype scale.
(Requested as S6-S14; the source labels them G6-G14 - identical 9 downstream gauges.)

  id   x_m       y_m      at_lab_s   ws_lab_m
  G6   4947.4    4289.7   10.2       84.2
  G7   5717.3    4407.6   102        49.1
  G8   6775.1    3869.2   182        54.0
  G9   7128.2    3162.0   263        40.2
  G10  8585.3    3443.1   404        34.9
  G11  9675.0    3085.9   600        27.4
  G12  10939.1   3044.8   845        21.5
  G13  11724.4   2810.4   972        16.1
  G14  12723.7   2485.1   1139       12.9

--------------------------------------------------------------------------------
## 5. PUBLISHED COMPUTED-vs-OBSERVED (target numbers for our run)

One fully worked open comparison is Biscarini et al. 2016 (OpenFOAM 3D VOF, k-epsilon). Their
model results (labelled WS-3D / AT3D) are in observations.json under `published_model_comparison`
- these are ONE model's output, not observations. Their reported agreement: police-point water
levels within ~5% except P5, P10, P16, P17 (7-10%); transformer arrival A=100, B=1175, C=1425 s
vs observed 100/1240/1420.

The TELEMAC reference solution for the small mesh is shipped as f2d_malpasset-small.slf (in this
dir) - the expected TELEMAC-2D output to reproduce (the validation harness malpasset.xml compares
a fresh run's last frame against it to 1e-2 tolerance).

--------------------------------------------------------------------------------
## 6. CRS / DATUM (exactly as stated by sources)

- Biscarini et al. 2016: figures/tables are in "a local coordinate system"; "Following the
  information provided by EDF, and consistently with the reference system of the map, the dam is
  considered as a straight line between the points of coordinates (4701, 4143) and (4655, 4392)."
  -> horizontal frame = a LOCAL planar metric system, NOT a named EPSG projection. All obs points
     AND the TELEMAC mesh share this frame (dam nodes match the mesh: 4701.18,4143.41 / 4655.5,4392.10).
- Vertical: maximum water-surface ELEVATIONS in metres (French NGF datum is the standard for this
  dataset; not restated per-table in the source). Reservoir initial free-surface commonly cited at
  100 m (TUFLOW 2019 "Initial water level: 100 m AD").
- Approximate real-world anchor (NOT a published transform): the dam is at ~43.5072 N, 6.7539 E
  (UTM 32N approx E=318435 N=4819592), per the anuga_malpasset georeferencing repo; use only for
  rough basemap overlay, not for reprojecting the obs.

--------------------------------------------------------------------------------
## 7. SOURCE URLS

- TELEMAC case files (GitHub mirror): https://github.com/ogoe/OpenTelemac/tree/master/examples/telemac2d/malpasset
- Canonical upstream: https://gitlab.pam-retd.fr/otm/telemac-mascaret  (opentelemac.org)
- Biscarini et al. 2016, Water 8:545: https://doi.org/10.3390/w8110545  (open access)
  archived HTML used: https://web.archive.org/web/20260113105427/https://www.mdpi.com/2073-4441/8/11/545
- Hervouet & Petitjean 1999, JHR 37(6):777-788: https://doi.org/10.1080/00221689909498511 (paywall)
- FullSWOF (friction n=0.033 corroboration): https://arxiv.org/abs/1401.4125
- TUFLOW benchmarking (transformer B discrepancy): https://www.tuflow.com/insights/2019_03-malpasset/
- ANUGA malpasset georeferencing (CRS anchor): https://github.com/stoiver/anuga_malpasset
- David L. George Malpasset project (context): https://dlgeorge.github.io/project/malpasset-project
