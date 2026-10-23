"""Logo files must be plain SVG: a byte-order mark makes Streamlit's st.logo reject them."""
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parent.parent / "assets"


@pytest.mark.parametrize("name", ["logo.svg", "logo_full.svg", "logo_full_dark.svg"])
def test_logo_is_plain_svg(name):
    data = (ASSETS / name).read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), f"{name} starts with a UTF-8 BOM"
    assert data.lstrip().startswith(b"<svg")
