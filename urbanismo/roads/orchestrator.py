from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from shapely.ops import unary_union

from .blocks import blocks_from_roads_mask
from .evaluate import evaluate_roads
from .geometry import make_transformers, to_shapely, transform_geom
from .grid import generate_grid_roads
from .rows_policy import apply_rows_policy


def _signature(candidate: Dict[str, Any]) -> Tuple:
    """
    Assinatura para remover duplicados:
      - angle_deg arredondado
      - kx/ky
      - roads_area_m2 aproximado (se houver)
    """
    dbg = candidate.get("debug", {}) or {}
    angle = round(float(dbg.get("angle_deg", 0.0)), 2)
    kx = int(dbg.get("kx", 0) or 0)
    ky = int(dbg.get("ky", 0) or 0)
    # assinatura sem área (ainda não avaliou) — já ajuda muito
    return (angle, kx, ky)


def generate_and_rank_road_networks(
    *,
    al_wgs_geojson: Dict[str, Any],
    params: Dict[str, Any],
    srid_calc: int = 3857,
    targets: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    targets = targets or {}

    # Políticas (dropdown)
    rows_policy = str(params.get(
        "rows_policy", "edge_1_interior_prefer_2") or "edge_1_interior_prefer_2")
    edge_band_m = float(params.get("edge_band_m", params.get(
        "edge_band_factor", 1.5) or 1.5) or 1.5)
    top_band_m = float(params.get("top_band_m", params.get(
        "top_band_factor", 1.5) or 1.5) or 1.5)

    # edge_band/top_band em metros: fator * prof_ideal (fallback 30)
    prof_ideal = float(params.get(
        "prof_ideal_m", params.get("prof_min_m", 30.0)) or 30.0)
    edge_band_m = edge_band_m * prof_ideal
    top_band_m = top_band_m * prof_ideal

    # base para descobrir ângulo dominante
    base = generate_grid_roads(
        al_wgs_geojson=al_wgs_geojson,
        params=params,
        srid_calc=srid_calc,
        variant="mid",
    )
    base_angle = float((base.get("debug") or {}).get("angle_deg", 0.0))

    variants = ["low", "mid", "high"]

    raw_candidates: List[Dict[str, Any]] = []

    for v in variants:
        c = generate_grid_roads(
            al_wgs_geojson=al_wgs_geojson,
            params=params,
            srid_calc=srid_calc,
            angle_override_deg=base_angle,
            variant=v,
        )
        raw_candidates.append({"id": f"grid_{v}", "strategy": "grid", **c})

    for v in variants:
        c = generate_grid_roads(
            al_wgs_geojson=al_wgs_geojson,
            params=params,
            srid_calc=srid_calc,
            angle_override_deg=base_angle + 90.0,
            variant=v,
        )
        raw_candidates.append({"id": f"grid90_{v}", "strategy": "grid90", **c})

    # ✅ dedup simples por assinatura
    seen = set()
    candidates: List[Dict[str, Any]] = []
    for c in raw_candidates:
        sig = _signature(c)
        if sig in seen:
            continue
        seen.add(sig)
        candidates.append(c)

    # Preparar AOI em metros para rows_policy
    tf = make_transformers(srid_calc)
    aoi_wgs = to_shapely(al_wgs_geojson)
    aoi_m = transform_geom(aoi_wgs, tf.wgs_to_m)

    ranked: List[Dict[str, Any]] = []

    for c in candidates:
        ev = evaluate_roads(
            al_wgs_geojson=al_wgs_geojson,
            roads_mask_fc=c["roads_mask_fc"],
            srid_calc=srid_calc,
            targets=targets,
        )

        # blocks
        blk = blocks_from_roads_mask(
            al_wgs_geojson=al_wgs_geojson,
            roads_mask_fc=c["roads_mask_fc"],
            srid_calc=srid_calc,
            min_block_area_m2=float(targets.get("min_block_area_m2", 200.0)),
        )

        # rows policy (programa por bloco)
        # Para o MVP, usamos prof_min_m = prof_floor (área/frente) como piso.
        frente = float(params.get("frente_min_m", 10.0) or 10.0)
        area_min = float(params.get("area_min_m2", 200.0) or 200.0)
        prof_min_m = max(area_min / max(frente, 0.01), prof_ideal * 0.7)

        # transforma blocks para metros para calcular widths (usamos propriedades do shape em metros)
        # aqui, como blocks_from_roads_mask retorna WGS, vamos re-shapely e transformar pra m
        blocks_m = []
        for f in (blk["blocks_fc"].get("features") or []):
            g = to_shapely(f)
            gm = transform_geom(g, tf.wgs_to_m)
            if gm and not gm.is_empty:
                blocks_m.append(gm)

        program = apply_rows_policy(
            blocks_m=blocks_m,
            aoi_m=aoi_m,
            policy=rows_policy,
            prof_ideal_m=prof_ideal,
            prof_min_m=prof_min_m,
            edge_band_m=edge_band_m,
            top_band_m=top_band_m,
        )

        ranked.append(
            {
                "id": c["id"],
                "strategy": c["strategy"],
                "roads_mask_fc": c["roads_mask_fc"],
                "roads_axis_fc": c["roads_axis_fc"],
                "blocks_fc": blk["blocks_fc"],
                "block_program": program,
                "debug": {
                    **(c.get("debug") or {}),
                    "rows_policy": rows_policy,
                    "edge_band_m": edge_band_m,
                    "top_band_m": top_band_m,
                },
                "score": ev["score"],
                "metrics": ev["metrics"],
                "penalties": ev["penalties"],
            }
        )

    ranked.sort(key=lambda x: x["score"], reverse=True)
    best = ranked[0] if ranked else None
    return {"best": best, "ranked": ranked}
