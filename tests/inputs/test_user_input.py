"""A string handed to a vertex row is refused as an unknown layer as well as a bad geometry."""

import pytest

from trid3nt_server.inputs.user_input import UserInputError, polyline_coords


def test_unknown_layer_name_is_named_in_the_refusal():
    with pytest.raises(UserInputError, match="is not a layer on the map and is not a list"):
        polyline_coords("input-src-abc", label="domain")
