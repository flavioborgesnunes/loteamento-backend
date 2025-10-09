import math
from typing import Dict, Tuple, List
from shapely.geometry import shape, mapping, LineString, Polygon, MultiPolygon, box
from shapely.ops import unary_union, split, linemerge
from shapely import affinity
from shapely.ops import transform as shp_transform

from pyproj import Transformer

def _ensure_multipolygon(g):
    if isinstance(g, Polygon):
        return MultiPolygon([g])
    if isinstance(g, MultiPolygon):
        return g
    raise ValueError("AL deve ser Polygon/MultiPolygon")

def _transform_geom(geom, src_srid:int, dst_srid:int):
    tf = Transformer.from_crs(src_srid, dst_srid, always_xy=True)
    def _coords_iter(g):
        if isinstance(g, (Polygon,)):
            return [list(g.exterior.coords)] + [list(r.coords) for r in g.interiors]
        return []
    return shapely_transform(geom, tf)

def shapely_transform(geom, transformer: Transformer):
    """Aplicar transformação de coordenadas em geometria shapely."""
    def _tx_xy(x, y, z=None):
        x2, y2 = transformer.transform(x, y)
        return (x2, y2) if z is None else (x2, y2, z)
    return shp_transform(_tx_xy, geom)

def estimate_orientation_deg(geom_m):
    """Estimativa simples pelo retângulo mínimo (OBB)."""
    try:
        minrect = geom_m.minimum_rotated_rectangle
        coords = list(minrect.exterior.coords)
        # pega a aresta mais longa
        max_d, ang = 0, 0
        for i in range(len(coords)-1):
            x1,y1 = coords[i]; x2,y2 = coords[i+1]
            dx, dy = (x2-x1, y2-y1)
            d = math.hypot(dx, dy)
            if d > max_d:
                max_d = d
                ang = math.degrees(math.atan2(dy, dx))
        return ang % 180.0
    except Exception:
        return 0.0

def _gen_parallel_lines_covering_bbox(bbox, spacing, angle_deg, center):
    """Gera uma família de linhas paralelas cobrindo o bbox, com espaçamento (m)."""
    minx, miny, maxx, maxy = bbox
    W, H = (maxx-minx, maxy-miny)
    diag = math.hypot(W, H) + spacing*2
    cx, cy = center
    # Linha base horizontal centrada
    base = LineString([(cx - diag/2, cy), (cx + diag/2, cy)])
    base = affinity.rotate(base, angle_deg, origin=(cx, cy), use_radians=False)
    # Itera deslocando ortogonalmente
    ortho = angle_deg + 90
    lines = []
    # quantidade suficiente para cobrir bbox
    n = int((max(W, H) + diag) / spacing) + 4
    for k in range(-n, n+1):
        l = affinity.translate(base,
                               xoff=math.cos(math.radians(ortho))*k*spacing,
                               yoff=math.sin(math.radians(ortho))*k*spacing)
        lines.append(l)
    return lines

def buffer_lines_as_corridors(lines:List[LineString], width_m:float):
    half = width_m/2.0
    return [l.buffer(half, cap_style=2, join_style=2) for l in lines]

def build_road_and_blocks(al_wgs_geojson:dict, params:dict, srid_calc:int=3857)->Tuple[dict, dict]:
    """
    Retorna (vias_fc, quarteiroes_fc) em WGS84.
    Heurística: vias 'verticais' espaçadas por (2*prof_min + larg_rua_vert).
                travessas horizontais a cada 'compr_max_quarteirao'.
    """
    al_wgs = _ensure_multipolygon(shape(al_wgs_geojson))
    # reprojeta para métrico
    tf_wgs_to_m = Transformer.from_crs(4326, srid_calc, always_xy=True)
    tf_m_to_wgs = Transformer.from_crs(srid_calc, 4326, always_xy=True)
    al_m = shapely_transform(al_wgs, tf_wgs_to_m)

    frente_min = float(params["frente_min_m"])
    prof_min = float(params["prof_min_m"])
    larg_v = float(params["larg_rua_vert_m"])
    larg_h = float(params["larg_rua_horiz_m"])
    comp_max = float(params["compr_max_quarteirao_m"])
    orient = params.get("orientacao_graus")
    angle = float(orient) if orient is not None else estimate_orientation_deg(al_m)

    # --------- Gera vias "verticais" (familia paralela ao ângulo 'angle') ----------
    spacing_vias = 2*prof_min + larg_v
    cx, cy = (al_m.bounds[0] + (al_m.bounds[2]-al_m.bounds[0])/2,
              al_m.bounds[1] + (al_m.bounds[3]-al_m.bounds[1])/2)
    fam_vert = _gen_parallel_lines_covering_bbox(al_m.bounds, spacing_vias, angle, (cx, cy))

    # Corridors das vias verticais
    vias_vert_corr = buffer_lines_as_corridors(fam_vert, larg_v)
    vias_vert_corr = [poly.intersection(al_m) for poly in vias_vert_corr]
    vias_vert_corr = [p for p in vias_vert_corr if not p.is_empty]

    # --------- Travessas horizontais conforme comprimento máximo  ----------
    # Gerar família perpendicular (angle+90) com espaçamento 'comp_max'
    fam_horiz = _gen_parallel_lines_covering_bbox(al_m.bounds, comp_max, angle+90.0, (cx, cy))
    trav_corr = buffer_lines_as_corridors(fam_horiz, larg_h)
    trav_corr = [poly.intersection(al_m) for poly in trav_corr]
    trav_corr = [p for p in trav_corr if not p.is_empty]

    # Quarteirões = AL - (vias vert + travessas)
    all_corridors = unary_union(vias_vert_corr + trav_corr)
    quarteiroes = al_m.difference(all_corridors)
    quarteiroes = _ensure_multipolygon(quarteiroes)

    # Linhas centrais das vias para visual
    vias_lines = []
    for l in fam_vert:
        cl = l.intersection(al_m)
        if not cl.is_empty:
            vias_lines.append({"tipo":"vertical","largura_m":larg_v,"geom":cl})
    for l in fam_horiz:
        cl = l.intersection(al_m)
        if not cl.is_empty:
            vias_lines.append({"tipo":"horizontal","largura_m":larg_h,"geom":cl})

    # reprojeta de volta
    def to_wgs(geom): return shapely_transform(geom, tf_m_to_wgs)

    vias_fc = {
        "type":"FeatureCollection",
        "features":[
            {"type":"Feature","properties":{"tipo":v["tipo"],"largura_m":v["largura_m"]},
             "geometry":mapping(to_wgs(v["geom"]))}
            for v in vias_lines
        ]
    }
    quarteiroes_fc = {
        "type":"FeatureCollection",
        "features":[
            {"type":"Feature","properties":{},"geometry":mapping(to_wgs(q))}
            for q in quarteiroes.geoms
        ]
    }
    return vias_fc, quarteiroes_fc

def slice_lots(quarteiroes_fc:dict, params:dict, srid_calc:int=3857)->dict:
    """
    Fatiamento MVP: para cada quarteirão, corta com linhas perpendiculares ao ângulo (ou ao maior eixo),
    com espaçamento = frente_min. Não trata todos os cantos, mas entrega lotes válidos para teste.
    """
    frente_min = float(params["frente_min_m"])
    prof_min = float(params["prof_min_m"])
    orient = params.get("orientacao_graus")
    tf_wgs_to_m = Transformer.from_crs(4326, srid_calc, always_xy=True)
    tf_m_to_wgs = Transformer.from_crs(srid_calc, 4326, always_xy=True)

    features = []
    for feat in quarteiroes_fc.get("features", []):
        q_wgs = shape(feat["geometry"])
        q_m = shapely_transform(q_wgs, tf_wgs_to_m)
        angle = float(orient) if orient is not None else estimate_orientation_deg(q_m)
        # Lotes: linhas paralelas ao ângulo+90 a cada 'frente_min'
        cx, cy = q_m.centroid.x, q_m.centroid.y
        bbox = q_m.bounds
        fam = _gen_parallel_lines_covering_bbox(bbox, frente_min, angle+90.0, (cx, cy))
        # recorta em fatias
        current = q_m
        for cut in fam:
            try:
                current = split(current, cut)
            except Exception:
                continue
            if hasattr(current, "geoms"):
                # mantém multi para continuar cortando
                current = unary_union([g for g in current.geoms])
        # após cortes, coletar os pedaços
        pieces = []
        if hasattr(current, "geoms"):
            pieces = list(current.geoms)
        else:
            pieces = [current]

        # filtra por profundidade mínima (aproximada pela menor dimensão)
        for p in pieces:
            if p.is_empty: continue
            min_rect = p.minimum_rotated_rectangle
            coords = list(min_rect.exterior.coords)
            edges = [math.hypot(coords[i+1][0]-coords[i][0], coords[i+1][1]-coords[i][1]) for i in range(4)]
            small = min(edges)
            large = max(edges)
            prof_media = large  # aproximação
            frente_aprox = small
            if prof_media >= prof_min and frente_aprox >= frente_min:
                features.append({
                    "type":"Feature",
                    "properties":{"frente_aprox_m":round(frente_aprox,2),
                                  "prof_aprox_m":round(prof_media,2),
                                  "score_qualidade":1.0},
                    "geometry":mapping(shapely_transform(p, tf_m_to_wgs))
                })

    return {"type":"FeatureCollection","features":features}

def compute_preview(al_geojson:dict, params:dict)->Dict:
    vias_fc, quarteiroes_fc = build_road_and_blocks(al_geojson, params, params.get("srid_calc", 3857))
    lotes_fc = slice_lots(quarteiroes_fc, params, params.get("srid_calc", 3857))
    metrics = {
        "n_vias": len(vias_fc["features"]),
        "n_quarteiroes": len(quarteiroes_fc["features"]),
        "n_lotes": len(lotes_fc["features"]),
    }
    return {"vias":vias_fc, "quarteiroes":quarteiroes_fc, "lotes":lotes_fc, "metrics":metrics}
