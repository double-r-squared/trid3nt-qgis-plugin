"""Run script downloads are siblings of a selected run output."""

from plugin.net import trid3nt_client


def test_only_run_outputs_offer_the_script_and_lock():
    run = "01M833H3N0B90A6M7GJ3HND42S"
    pair = trid3nt_client.run_script_objects(f"s3://runs/{run}/nested/head.tif")
    assert pair == (run, f"s3://runs/{run}/case.py", f"s3://runs/{run}/trid3nt.lock")
    assert trid3nt_client.run_script_objects("s3://runs/inputs/mesh.tif") is None
    assert trid3nt_client.run_script_objects(f"https://runs/{run}/head.tif") is None
