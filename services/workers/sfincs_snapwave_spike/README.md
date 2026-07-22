# SFINCS + SnapWave + quadtree deck-authoring spike (Agent B gate)

Scratch only. NO repo edits outside this dir. Validates that a headless
quadtree + SnapWave deck can be HAND-AUTHORED (hydromt-sfincs 1.2.2 has no
`setup_snapwave` and no quadtree-from-scratch builder).

## Files
- `author_deck.py`  — hand-authors the deck into `deck/` from the SFINCS Fortran
  source contract (no `setup_snapwave` dependency).
- `validate_deck.py` — structural validation against `sfincs_quadtree.F90` (the
  Fortran reader) + the `snapwave_*` keyword set. Exit 0 = valid.
- `deck/` — the authored deck (quadtree netcdf + sfincs.inp + boundary files).

## Run
```
source ../../agent/.venv/bin/activate
python3 author_deck.py
python3 validate_deck.py   # exit 0
```

## Authoritative sources (SFINCS @ Deltares/SFINCS main)
- `source/src/sfincs_quadtree.F90` — quadtree netcdf reader: requires bare vars
  `n,m,level,md,md1,md2,mu,mu1,mu2,nd,nd1,nd2,nu,nu1,nu2,z,mask,snapwave_mask`
  on dim `mesh2d_nFaces` + global attrs `x0,y0,dx,dy,rotation,nmax,mmax,nr_levels`.
- `source/src/sfincs_snapwave.f90` — `snapwave_*` sfincs.inp keywords (lines 671-682).
- `source/src/sfincs_ncoutput.F90` — wave output vars `hm0,hm0ig,fwx,fwy,tp,tpig`.

## Key finding: two distinct "quadtree netcdf" shapes
- Fortran solver reads the bare `n/m/level` + neighbour-index table (this deck).
- hydromt `QuadtreeGrid` wraps a UGRID `mesh2d` topology (`data.ugrid.to_netcdf`).
  The round-trip "failure" in validate step 2 is expected and informative.

## Solve
Docker is not accessible non-interactively here (user not in `docker` group;
sudo needs a password). The real solve runs on AWS Batch (`deltares/sfincs-cpu`),
already proven for the pluvial path. The deck is staged the same way.
