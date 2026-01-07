from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from pyproj import Transformer
from shapely import affinity
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union


def to_shapely(geojson_geom_or_feature: Dict[str, Any]) -> BaseGeometry:
    """
    Aceita:
      - Feature {"type":"Feature","geometry":{...}}
      - Geometry {...}
    """
    if not geojson_geom_or_feature:
        raise ValueError("GeoJSON vazio")

    if geojson_geom_or_feature.get("type") == "Feature":
        geom = geojson_geom_or_feature.get("geometry")
    else:
        geom = geojson_geom_or_feature
    if not geom:
        raise ValueError("GeoJSON sem geometry")
    return shape(geom)


def ensure_multipolygon(g: BaseGeometry) -> BaseGeometry:
    # aceita Polygon/MultiPolygon
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return g
    # tentar converter collection -> union
    if hasattr(g, "geoms"):
        return unary_union(list(g.geoms))
    return g


def make_transformers(srid_calc: int):
    # assume input WGS84 (EPSG:4326)
    wgs_to_m = Transformer.from_crs(
        "EPSG:4326", f"EPSG:{srid_calc}", always_xy=True).transform
    m_to_wgs = Transformer.from_crs(
        f"EPSG:{srid_calc}", "EPSG:4326", always_xy=True).transform
    return type("TF", (), {"wgs_to_m": wgs_to_m, "m_to_wgs": m_to_wgs})


def transform_geom(g: BaseGeometry, fn) -> BaseGeometry:
    return transform(fn, g)


def estimate_orientation_deg(poly_m: BaseGeometry) -> float:
    """
    Orientação dominante aproximada usando o retângulo mínimo rotacionado.
    """
    r = poly_m.minimum_rotated_rectangle
    coords = list(r.exterior.coords)
    # pega o maior segmento do retângulo
    best = None
    best_len = -1.0
    for i in range(len(coords) - 1):
        (x1, y1), (x2, y2) = coords[i], coords[i + 1]
        dx, dy = (x2 - x1), (y2 - y1)
        L = (dx * dx + dy * dy) ** 0.5
        if L > best_len:
            best_len = L
            best = (dx, dy)
    if not best or best_len <= 0:
        return 0.0
    dx, dy = best
    import math
    ang = math.degrees(math.atan2(dy, dx))
    # normaliza 0..180
    ang = ang % 180.0
    return ang


def rotate_to_angle(g: BaseGeometry, angle_deg: float, origin: Tuple[float, float]) -> BaseGeometry:
    # gira para alinhar com eixo X (rotações em torno do centroid)
    return affinity.rotate(g, -angle_deg, origin=origin, use_radians=False)


def rotate_back(g: BaseGeometry, angle_deg: float, origin: Tuple[float, float]) -> BaseGeometry:
    return affinity.rotate(g, angle_deg, origin=origin, use_radians=False)


def line_across_bbox_x(x: float, miny: float, maxy: float):
    from shapely.geometry import LineString
    return LineString([(x, miny), (x, maxy)])


def line_across_bbox_y(y: float, minx: float, maxx: float):
    from shapely.geometry import LineString
    return LineString([(minx, y), (maxx, y)])


def safe_intersection(a: BaseGeometry, b: BaseGeometry) -> Optional[BaseGeometry]:
    if a is None or b is None:
        return None
    if a.is_empty or b.is_empty:
        return None
    try:
        g = a.intersection(b)
        if g.is_empty:
            return None
        return g
    except Exception:
        return None


def buffer_lines_as_corridors(lines_m: BaseGeometry, width_m: float) -> BaseGeometry:
    """
    Rua como POLÍGONO: buffer do eixo.
    width_m = largura total
    """
    half = float(width_m) / 2.0
    return lines_m.buffer(half, cap_style=2, join_style=2)


def to_feature(geom: BaseGeometry, props: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "Feature", "geometry": mapping(geom), "properties": props or {}}


def to_fc(features):
    return {"type": "FeatureCollection", "features": features or []}


def union_features_fc(fc: Dict[str, Any]) -> Optional[BaseGeometry]:
    if not fc or not fc.get("features"):
        return None
    geoms = []
    for f in fc["features"]:
        g = to_shapely(f)
        if g and not g.is_empty:
            geoms.append(g)
    if not geoms:
        return None
    return unary_union(geoms)
