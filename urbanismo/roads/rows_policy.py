from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

from shapely.geometry import Polygon

# Políticas suportadas (dropdown)
POLICIES = [
    "edge_1_interior_prefer_2",
    "top_1_interior_prefer_2",
    "first_last_1_interior_prefer_2",
    "prefer_2_by_width",
]


def _typical_width_m(block_m: Polygon) -> float:
    """
    Aproxima largura típica do quarteirão usando bbox mínimo rotacionado.
    (bom o suficiente pro MVP; depois você troca por amostragem real)
    """
    r = block_m.minimum_rotated_rectangle
    coords = list(r.exterior.coords)
    edges = []
    for i in range(len(coords) - 1):
        (x1, y1), (x2, y2) = coords[i], coords[i + 1]
        L = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        edges.append(L)
    edges.sort()
    # menor lado ~ largura, maior lado ~ comprimento
    if not edges:
        return 0.0
    return float(edges[0])


def _rows_by_width(width_m: float, prof_ideal_m: float, prof_min_m: float) -> int:
    """
    Regra objetiva:
      - >= 2*prof_ideal -> 2
      - >= 1*prof_min  -> 1
      - senão -> 0
    """
    if width_m >= 2.0 * prof_ideal_m:
        return 2
    if width_m >= 1.0 * prof_min_m:
        return 1
    return 0


def _is_top_block(block_centroid_y: float, y_max: float, top_band_m: float) -> bool:
    return (y_max - block_centroid_y) <= top_band_m


def _is_edge_block(block_poly: Polygon, aoi_poly: Polygon, edge_band_m: float) -> bool:
    """
    Edge = está perto da borda da AOI.
    Aproximação: se o polígono intersecta o buffer interno da AOI, não é borda.
    """
    if edge_band_m <= 0:
        return False
    inner = aoi_poly.buffer(-edge_band_m)
    # se o inner sumiu, tudo vira borda
    if inner.is_empty:
        return True
    # se o bloco está majoritariamente fora do inner, consideramos borda
    return not block_poly.within(inner)


def apply_rows_policy(
    *,
    blocks_m: List[Polygon],
    aoi_m: Polygon,
    policy: str,
    prof_ideal_m: float,
    prof_min_m: float,
    edge_band_m: float = 0.0,
    top_band_m: float = 0.0,
) -> List[Dict[str, Any]]:
    """
    Retorna uma lista de dicts (programa) com:
      - block_id
      - rows (0/1/2)
      - width_m
      - reason
    """
    if policy not in POLICIES:
        policy = "prefer_2_by_width"

    y_min, y_min2, x_max, y_max = aoi_m.bounds[1], aoi_m.bounds[1], aoi_m.bounds[2], aoi_m.bounds[3]

    # ordenação por "faixas" em Y (para first/last)
    blocks_with_info = []
    for i, b in enumerate(blocks_m):
        width = _typical_width_m(b)
        rows_auto = _rows_by_width(width, prof_ideal_m, prof_min_m)
        cy = float(b.centroid.y)
        blocks_with_info.append((i, b, width, rows_auto, cy))

    blocks_sorted_y = sorted(blocks_with_info, key=lambda t: t[4])
    if blocks_sorted_y:
        first_ids = {blocks_sorted_y[0][0]}
        last_ids = {blocks_sorted_y[-1][0]}
    else:
        first_ids, last_ids = set(), set()

    program = []

    for (i, b, width, rows_auto, cy) in blocks_with_info:
        rows = rows_auto
        reason = "auto_by_width"

        if policy == "prefer_2_by_width":
            # já está
            pass

        elif policy == "edge_1_interior_prefer_2":
            if _is_edge_block(b, aoi_m, edge_band_m):
                # borda sempre tenta 1 (se ao menos couber 1)
                rows = 1 if rows_auto >= 1 else 0
                reason = "edge_force_1"
            else:
                # interior prefere 2 (mas respeita auto)
                rows = 2 if rows_auto == 2 else rows_auto
                reason = "interior_prefer_2"

        elif policy == "top_1_interior_prefer_2":
            if _is_top_block(cy, y_max, top_band_m):
                rows = 1 if rows_auto >= 1 else 0
                reason = "top_force_1"
            else:
                rows = 2 if rows_auto == 2 else rows_auto
                reason = "rest_prefer_2"

        elif policy == "first_last_1_interior_prefer_2":
            if i in first_ids or i in last_ids:
                rows = 1 if rows_auto >= 1 else 0
                reason = "first_last_force_1"
            else:
                rows = 2 if rows_auto == 2 else rows_auto
                reason = "middle_prefer_2"

        program.append(
            {
                "block_id": i,
                "rows": int(rows),
                "width_m": float(width),
                "reason": reason,
            }
        )

    return program
