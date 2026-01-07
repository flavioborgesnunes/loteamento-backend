from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from .geometry import (ensure_multipolygon, make_transformers, to_fc,
                       to_feature, to_shapely, transform_geom,
                       union_features_fc)


def _explode_polygons(g) -> List[Polygon]:
    if g is None or g.is_empty:
        return []
    if isinstance(g, Polygon):
        return [g]
    if isinstance(g, MultiPolygon):
        return list(g.geoms)
    if hasattr(g, "geoms"):
        polys = []
        for part in g.geoms:
            if isinstance(part, Polygon):
                polys.append(part)
            elif isinstance(part, MultiPolygon):
                polys.extend(list(part.geoms))
        return polys
    return []


def blocks_from_roads_mask(
    *,
    al_wgs_geojson: Dict[str, Any],
    roads_mask_fc: Dict[str, Any],
    srid_calc: int = 3857,
    min_block_area_m2: float = 200.0,
    simplify_m: float = 0.0,
) -> Dict[str, Any]:
    """
    blocks = AOI - union(roads_mask)
    retorna FeatureCollection (WGS84) e métricas simples em debug.
    """
    tf = make_transformers(srid_calc)
    al_wgs = ensure_multipolygon(to_shapely(al_wgs_geojson))
    al_m = transform_geom(al_wgs, tf.wgs_to_m)

    roads_union_wgs = union_features_fc(roads_mask_fc)
    roads_union_m = transform_geom(
        roads_union_wgs, tf.wgs_to_m) if roads_union_wgs else None

    if roads_union_m is None or roads_union_m.is_empty:
        # sem vias: bloco é a própria AL
        blocks_m = al_m
    else:
        blocks_m = al_m.difference(roads_union_m)

    if simplify_m and simplify_m > 0:
        blocks_m = blocks_m.simplify(simplify_m)

    polys = [p for p in _explode_polygons(
        blocks_m) if p.area >= float(min_block_area_m2)]

    features = []
    for i, p in enumerate(polys):
        pw = transform_geom(p, tf.m_to_wgs)
        features.append(
            to_feature(pw, {"block_id": i, "area_m2": float(p.area)})
        )

    return {
        "blocks_fc": to_fc(features),
        "debug": {
            "n_blocks": len(features),
            "min_block_area_m2": float(min_block_area_m2),
        },
    }
