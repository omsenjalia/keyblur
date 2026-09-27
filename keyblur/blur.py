"""Elliptical Gaussian blur compositing, shared by live preview and export."""
from __future__ import annotations

from typing import Iterable

import cv2
import numpy as np

from .model import BlurState

FEATHER_FRAC = 0.06  # soft edge as a fraction of the smaller radius


def apply_blurs(frame: np.ndarray, states: Iterable[BlurState], px_scale: float = 1.0) -> np.ndarray:
    """Return a copy of ``frame`` (HxWx3 uint8) with each state's ellipse blurred.

    ``px_scale`` converts strength (Gaussian sigma in *source* pixels) to the
    pixel size of ``frame`` - e.g. 0.5 for a half-width preview proxy.
    """
    states = list(states)
    if not states:
        return frame
    out = frame.copy()
    h, w = out.shape[:2]
    for s in states:
        sigma = s.strength * px_scale
        rx = s.radius_x * w
        ry = s.radius_y * h
        if sigma < 0.3 or rx < 1 or ry < 1:
            continue
        cx = s.x * w
        cy = s.y * h
        pad = int(np.ceil(3 * sigma)) + 2
        x0 = max(0, int(np.floor(cx - rx)) - pad)
        y0 = max(0, int(np.floor(cy - ry)) - pad)
        x1 = min(w, int(np.ceil(cx + rx)) + pad)
        y1 = min(h, int(np.ceil(cy + ry)) + pad)
        if x1 <= x0 or y1 <= y0:
            continue
        roi = out[y0:y1, x0:x1]
        blurred = cv2.GaussianBlur(roi, (0, 0), sigmaX=sigma, sigmaY=sigma,
                                   borderType=cv2.BORDER_REFLECT)

        # Feathered elliptical mask. The feather sits *inside* the ellipse so
        # the full-strength area never exceeds what the user drew by much.
        feather = max(1.0, FEATHER_FRAC * min(rx, ry))
        mask = np.zeros((y1 - y0, x1 - x0), np.float32)
        center = (int(round((cx - x0) * 16)), int(round((cy - y0) * 16)))
        axes = (max(1, int(round(rx * 16))), max(1, int(round(ry * 16))))
        cv2.ellipse(mask, center, axes, 0, 0, 360, 1.0, thickness=-1,
                    lineType=cv2.LINE_AA, shift=4)
        mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=feather / 2)
        alpha = mask[..., None]
        roi[:] = (blurred.astype(np.float32) * alpha
                  + roi.astype(np.float32) * (1.0 - alpha) + 0.5).astype(np.uint8)
    return out
