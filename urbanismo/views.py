# urbanismo/views.py
from __future__ import annotations

import math

from parcelamento.services import compute_preview, estimate_orientation_deg
from pyproj import Transformer
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from shapely.geometry import LineString, shape
from shapely.ops import transform as shp_transform


def _fc_count(fc):
    try:
        return len((fc or {}).get("features") or [])
    except Exception:
        return 0


def _simple_score(preview: dict) -> float:
    """
    Score simples (ajuste depois se quiser):
    + quarteirões
    + vias
    - áreas vazias (penaliza sobras)
    """
    m = (preview or {}).get("metrics") or {}
    n_q = float(m.get("n_quarteiroes") or _fc_count(
        preview.get("quarteiroes")))
    n_v = float(m.get("n_vias") or _fc_count(preview.get("vias")))
    n_z = float(m.get("n_areas_vazias") or _fc_count(
        preview.get("areas_vazias")))
    return (3.0 * n_q) + (1.0 * n_v) - (2.0 * n_z)


def _normalize_angle_deg(a: float) -> float:
    # normaliza pra [-180, 180)
    a = (a + 180.0) % 360.0 - 180.0
    return a


def _estimate_base_angle_from_al(al_geojson: dict, srid_calc: int) -> float:
    """
    Repete o essencial do compute_preview pra achar a orientação base,
    mas sem depender de o compute_preview devolver esse valor.
    """
    geom_mapping = al_geojson
    if isinstance(geom_mapping, dict) and geom_mapping.get("type") == "Feature":
        geom_mapping = geom_mapping.get("geometry") or geom_mapping

    # SRID_INPUT no seu projeto está 4674 (como você já usa no motor)
    tf_in_to_m = Transformer.from_crs(4674, srid_calc, always_xy=True)
    al_m = shp_transform(tf_in_to_m.transform, shape(geom_mapping))
    return float(estimate_orientation_deg(al_m))


def _normalize_angle_0_180(a: float) -> float:
    a = a % 180.0
    if a < 0:
        a += 180.0
    return a


def _angle_of_segment(p0, p1) -> float:
    dx = float(p1[0] - p0[0])
    dy = float(p1[1] - p0[1])
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return 0.0
    ang = math.degrees(math.atan2(dy, dx))
    return _normalize_angle_0_180(ang)


def _candidate_angles_from_al(al_geojson: dict, srid_calc: int, top_k: int = 3) -> list[float]:
    """
    Retorna 1..top_k ângulos (0..180) paralelos aos maiores lados da AL.
    """
    geom_mapping = al_geojson
    if isinstance(geom_mapping, dict) and geom_mapping.get("type") == "Feature":
        geom_mapping = geom_mapping.get("geometry") or geom_mapping

    tf_in_to_m = Transformer.from_crs(4674, srid_calc, always_xy=True)
    al_m = shp_transform(tf_in_to_m.transform, shape(geom_mapping))

    # garante polygon/multipolygon
    if al_m.geom_type == "MultiPolygon":
        poly = max(list(al_m.geoms), key=lambda g: g.area)
    elif al_m.geom_type == "Polygon":
        poly = al_m
    else:
        return [0.0]

    coords = list(poly.exterior.coords)
    segs = []
    for i in range(len(coords) - 1):
        p0 = coords[i]
        p1 = coords[i + 1]
        ln = LineString([p0, p1])
        L = float(ln.length)
        if L <= 0:
            continue
        ang = _angle_of_segment(p0, p1)
        segs.append((L, ang))

    if not segs:
        return [0.0]

    segs.sort(key=lambda x: x[0], reverse=True)

    # cluster simples por proximidade angular (evita ângulos quase iguais)
    chosen = []
    for L, ang in segs:
        if all(abs(ang - a) > 6.0 for a in chosen):  # 6° tolerância
            chosen.append(ang)
        if len(chosen) >= top_k:
            break

    return chosen or [segs[0][1]]


def _compmax_variants(base: float) -> list[float]:
    """
    Gera variações de dimensão (compr_max_quarteirao_m), priorizando reduzir um pouco
    para melhorar encaixe (ex: 200 -> 190 -> 180 ...).
    """
    base = float(base)
    if base <= 0:
        return [120.0, 110.0, 100.0]

    steps = [0, -10, -20, -30, -40, -50]
    out = []
    for s in steps:
        v = base + s
        if v >= base * 0.6:   # não derruba demais
            out.append(round(v, 3))
    # também testa +10 (às vezes resolve sobras)
    out.append(round(base + 10.0, 3))
    # dedup mantendo ordem
    seen = set()
    res = []
    for v in out:
        if v not in seen and v > 0:
            seen.add(v)
            res.append(v)
    return res


class RoadsPreviewView(APIView):
    """
    /api/roads/preview/
    Gateway compat: recebe payload legado, mas agora gera N sugestões
    (ranked list) usando o novo motor compute_preview().
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        body = request.data or {}
        al_geojson = body.get("al_geojson") or body.get("al_geom")
        params = (body.get("params") or {}).copy()

        if not al_geojson:
            return Response(
                {"detail": "al_geojson não informado."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        srid_calc = int(params.get("srid_calc", 3857))
        n_sug = int(body.get("n_sugestoes") or params.get("n_sugestoes") or 6)
        n_sug = max(1, min(n_sug, 12))  # trava pra não explodir

        # 1) ângulos candidatos: somente paralelos aos maiores lados da AL
        angles = _candidate_angles_from_al(al_geojson, srid_calc, top_k=3)

        # se usuário mandou orientacao_graus, força como único candidato
        if params.get("orientacao_graus") is not None:
            try:
                angles = [float(params["orientacao_graus"]) % 180.0]
            except Exception:
                pass

        # 2) variações por DIMENSÃO (compr_max_quarteirao_m), não por ângulo inventado
        base_comp_max = float(params.get("compr_max_quarteirao_m", 120))
        compmax_list = _compmax_variants(base_comp_max)

        extremidades_opts = [True, False]

        candidates = []
        seen_signatures = set()

        for ang in angles:
            for comp_max in compmax_list:
                for ext in extremidades_opts:
                    p = params.copy()
                    p["orientacao_graus"] = float(ang) % 180.0
                    p["compr_max_quarteirao_m"] = float(comp_max)
                    p["forcar_quarteirao_nas_extremidades"] = bool(ext)

                preview = compute_preview(al_geojson, p)
                score = _simple_score(preview)

                # assinatura leve pra deduplicar resultados idênticos
                m = preview.get("metrics") or {}
                sig = (
                    int(m.get("n_vias", _fc_count(preview.get("vias")))),
                    int(m.get("n_quarteiroes", _fc_count(
                        preview.get("quarteiroes")))),
                    int(m.get("n_areas_vazias", _fc_count(
                        preview.get("areas_vazias")))),
                    int(m.get("n_calcadas", _fc_count(preview.get("calcadas")))),
                    int(round(p["orientacao_graus"])),
                    int(p["forcar_quarteirao_nas_extremidades"]),
                )
                if sig in seen_signatures:
                    continue
                seen_signatures.add(sig)

                item_id = f"parcelamento_v1_a{int(round(p['orientacao_graus']))}_c{int(round(p['compr_max_quarteirao_m']))}_ext{int(ext)}"

                candidates.append(
                    {
                        "id": item_id,
                        "strategy": "parcelamento",
                        "score": score,
                        "metrics": m,
                        "debug": {
                            "compat": True,
                            "orientacao_graus": p["orientacao_graus"],
                            "forcar_quarteirao_nas_extremidades": bool(ext),
                            "vias": _fc_count(preview.get("vias")),
                            "quarteiroes": _fc_count(preview.get("quarteiroes")),
                            "calcadas": _fc_count(preview.get("calcadas")),
                            "areas_vazias": _fc_count(preview.get("areas_vazias")),
                        },
                        "preview": preview,

                        # compat antigo
                        "roads_axis_fc": preview.get("vias"),
                        "roads_mask_fc": preview.get("vias_area"),
                        "blocks_fc": preview.get("quarteiroes"),
                    }
                )

        # 3) ordenar, cortar top N
        candidates.sort(key=lambda x: float(
            x.get("score") or 0.0), reverse=True)
        ranked = candidates[:n_sug] if candidates else []

        best = ranked[0] if ranked else None

        return Response(
            {
                "ranked": ranked,
                "best": best,
            },
            status=200,
        )
