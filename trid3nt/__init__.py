"""The TRID3NT SDK: the domain - fetchers and derives, the mesh, the engines and their templates, the typed inputs,
the format set, the store, settings and credentials. It imports neither trid3nt_harness nor trid3nt_server."""

__all__ = ["case", "view"]


def __getattr__(name: str):
    # The front is imported on first use, so importing any one module of the SDK loads no template.
    if name == "case":
        from trid3nt.sdk import case

        return case
    if name == "view":
        from trid3nt.sdk.view import view

        return view
    raise AttributeError(f"module 'trid3nt' has no attribute {name!r}")
