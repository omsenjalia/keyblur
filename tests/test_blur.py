import numpy as np

from keyblur.blur import apply_blurs
from keyblur.model import BlurState


def noise(h=200, w=300):
    rng = np.random.default_rng(0)
    return rng.integers(0, 256, (h, w, 3), dtype=np.uint8)


def test_inside_changes_outside_untouched():
    img = noise()
    out = apply_blurs(img, [BlurState(0.5, 0.5, 0.1, 0.15, 8)])
    assert out is not img
    cy, cx = 100, 150
    inside = np.abs(out[cy - 10:cy + 10, cx - 10:cx + 10].astype(int) - img[cy - 10:cy + 10, cx - 10:cx + 10])
    assert inside.mean() > 20
    # radius 30x30 px; everything well outside the ellipse must be identical
    mask = np.ones(img.shape[:2], bool)
    mask[cy - 40:cy + 40, cx - 40:cx + 40] = False
    assert np.array_equal(out[mask], img[mask])


def test_zero_strength_is_noop():
    img = noise()
    out = apply_blurs(img, [BlurState(0.5, 0.5, 0.2, 0.2, 0)])
    assert np.array_equal(out, img)


def test_no_states_returns_input():
    img = noise()
    assert apply_blurs(img, []) is img


def test_region_at_edge_and_offscreen():
    img = noise()
    apply_blurs(img, [BlurState(0.0, 0.0, 0.2, 0.2, 5), BlurState(2.0, 2.0, 0.1, 0.1, 5)])


def test_scale_consistency():
    """A half-size proxy with px_scale=0.5 approximates the downscaled full-res result."""
    import cv2
    img = noise(400, 600)
    s = [BlurState(0.5, 0.5, 0.2, 0.2, 12)]
    full = cv2.resize(apply_blurs(img, s, 1.0), (300, 200), interpolation=cv2.INTER_AREA)
    proxy = apply_blurs(cv2.resize(img, (300, 200), interpolation=cv2.INTER_AREA), s, 0.5)
    assert np.abs(full[90:110, 140:160].astype(int) - proxy[90:110, 140:160]).mean() < 6
