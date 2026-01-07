from __future__ import annotations

from typing import Any, Dict, Optional

from shapely.ops import unary_union

from .blocks import blocks_from_roads_mask
from .geometry import (make_transformers, to_shapely, transform_geom,
                       union_features_fc)


def evaluate_roads(
    *,
    al_wgs_geojson: Dict[str, Any],
    roads_mask_fc: Dict[str, Any],
    srid_calc: int = 3857,
    targets: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    targets = targets or {}
    tf = make_transformers(srid_calc)

    aoi_wgs = to_shapely(al_wgs_geojson)
    aoi_m = transform_geom(aoi_wgs, tf.wgs_to_m)
    aoi_area = float(aoi_m.area) if aoi_m and not aoi_m.is_empty else 1.0

    roads_union_wgs = union_features_fc(roads_mask_fc)
    roads_union_m = transform_geom(
        roads_union_wgs, tf.wgs_to_m) if roads_union_wgs else None
    roads_area = float(
        roads_union_m.area) if roads_union_m and not roads_union_m.is_empty else 0.0
    ratio_vias = roads_area / aoi_area

    # blocos
    blk = blocks_from_roads_mask(
        al_wgs_geojson=al_wgs_geojson,
        roads_mask_fc=roads_mask_fc,
        srid_calc=srid_calc,
        min_block_area_m2=float(targets.get("min_block_area_m2", 200.0)),
    )
    n_blocks = int(blk["debug"]["n_blocks"])

    # score simples por enquanto (você troca pesos depois)
    ratio_min = float(targets.get("ratio_vias_min", 0.06))
    ratio_max = float(targets.get("ratio_vias_max", 0.22))

    penalties = {}
    score = 1.0

    # penaliza vias fora do range
    if ratio_vias < ratio_min:
        penalties["vias_too_low"] = ratio_min - ratio_vias
        score -= (ratio_min - ratio_vias) * 5.0
    if ratio_vias > ratio_max:
        penalties["vias_too_high"] = ratio_vias - ratio_max
        score -= (ratio_vias - ratio_max) * 5.0

    # penaliza fragmentação baixa (poucos blocos) em áreas grandes
    big_area_m2 = float(targets.get("big_area_m2", 250000))
    if aoi_area >= big_area_m2 and n_blocks <= 1:
        penalties["too_few_blocks"] = 1
        score -= 0.5

    metrics = {
        "aoi_area_m2": aoi_area,
        "roads_area_m2": roads_area,
        "ratio_vias": ratio_vias,
        "n_blocks": n_blocks,
    }

    return {"score": float(score), "metrics": metrics, "penalties": penalties}
