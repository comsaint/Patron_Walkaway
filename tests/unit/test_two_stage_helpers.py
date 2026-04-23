import numpy as np

from trainer.training.two_stage import (
    A4_FUSION_MODE_PRODUCT,
    candidate_cutoff_from_threshold,
    candidate_mask_from_scores,
    fuse_product_scores,
    validate_fusion_mode,
)


def test_validate_fusion_mode_falls_back_to_product() -> None:
    assert validate_fusion_mode("product") == A4_FUSION_MODE_PRODUCT
    assert validate_fusion_mode("PRODUCT") == A4_FUSION_MODE_PRODUCT
    assert validate_fusion_mode("unknown") == A4_FUSION_MODE_PRODUCT


def test_candidate_cutoff_is_bounded_and_scaled() -> None:
    assert candidate_cutoff_from_threshold(0.5, 0.9) == 0.45
    assert candidate_cutoff_from_threshold(10.0, 10.0) <= 0.99
    assert candidate_cutoff_from_threshold(-1.0, -1.0) >= 0.01


def test_candidate_mask_from_scores() -> None:
    scores = np.asarray([0.2, 0.4, 0.9], dtype=np.float64)
    mask = candidate_mask_from_scores(scores, cutoff=0.4)
    assert mask.tolist() == [False, True, True]


def test_fuse_product_scores() -> None:
    s1 = np.asarray([0.8, 0.5, 0.1], dtype=np.float64)
    s2 = np.asarray([0.5, 1.0, 0.0], dtype=np.float64)
    out = fuse_product_scores(s1, s2)
    assert np.allclose(out, np.asarray([0.4, 0.5, 0.0], dtype=np.float64))

