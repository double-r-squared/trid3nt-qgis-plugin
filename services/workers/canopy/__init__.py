"""GRACE-2 canopy-height ML-inference AWS Batch worker package.

The CLOUD LANE for the canopy-height tool (``compute_canopy_height``): a
containerized one-shot Batch job that downloads a staged sub-metre RGB COG +
Meta's pretrained HighResCanopyHeight weights (baked into the image), runs the
ViT+DPT inference via OpenGeoAI's ``geoai`` wrapper, writes a single-band
float32 canopy-height-in-metres COG + ``completion.json`` to the runs bucket.

Mirrors the OpenQuake / SWAN worker shape (the worker contract is
solver-agnostic); only the inference + the COG-write differ.
"""
