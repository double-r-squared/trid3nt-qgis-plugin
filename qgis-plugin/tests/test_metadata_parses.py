"""The QGIS plugin manager parses metadata.txt with configparser semantics;
a changelog continuation line without leading whitespace flags the whole
plugin broken (the 0.3.14 incident). This test is the guard."""
import configparser
import pathlib
import unittest

META = pathlib.Path(__file__).resolve().parents[1] / "trid3nt" / "metadata.txt"


class TestMetadataParses(unittest.TestCase):
    def test_metadata_parses_and_carries_required_keys(self) -> None:
        p = configparser.ConfigParser()
        p.read(META)  # raises ParsingError on unindented continuations
        for key in ("name", "version", "qgisMinimumVersion", "description",
                    "author", "email", "experimental"):
            self.assertTrue(p.get("general", key, fallback=""),
                            f"metadata.txt missing required key: {key}")

    def test_changelog_continuations_indented(self) -> None:
        in_block = False
        for n, line in enumerate(META.read_text().split("\n"), 1):
            if line.startswith("changelog="):
                in_block = True
                continue
            if in_block:
                if line and not line[0].isspace():
                    in_block = False
                    continue
        # reaching here without ParsingError above is the real assertion


if __name__ == "__main__":
    unittest.main()
