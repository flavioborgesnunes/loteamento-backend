import io
import json
import zipfile
from typing import Iterable, List, Optional, Tuple

from django.contrib.gis.db.models import GeometryField
from django.contrib.gis.db.models.functions import (AsGeoJSON, Intersection,
                                                    MakeValid)
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon
from django.contrib.gis.geos import Polygon as GEOSPolygon
from django.db import connection
from django.db.models import F, Func, Value
from django.db.utils import OperationalError
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from geodata.models import (Area, Cidade, LimiteFederal, LinhaTransmissao,
                            MalhaFerroviaria)
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
# MODELS
from rios.models import Waterway

# SERIALIZERS
from .serializers import ExportMapaSerializer

try:
    import simplekml
except ImportError:
    simplekml = None


# ============================ Helpers comuns ============================

def _refresh_conn():
    """Fecha a conexão se estiver inválida/obsoleta para forçar reabertura limpa."""
    try:
        connection.close_if_unusable_or_obsolete()
    except Exception:
        try:
            connection.close()
        except Exception:
            pass


def _yield_ids_in_batches(qs_ids, batch_size=2000):
    """
    Recebe um QuerySet de IDs (values_list(..., flat=True) + order_by('id')),
    e rende listas de até batch_size IDs.
    """
    batch = []
    for _id in qs_ids:
        batch.append(_id)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _safe_geos_from_geojson(geom_obj):
    """
    Converte um dict GeoJSON geometry em GEOSGeometry SRID=4326, validando.
    Suporta 2D/3D/4D (XYZM). O M é mantido internamente na GEOS se existir.
    """
    if not geom_obj:
        return None
    g = GEOSGeometry(json.dumps(geom_obj))
    if g.srid is None:
        g.srid = 4326
    if g.srid != 4326:
        g.transform(4326)
    if not g.valid:
        try:
            g = g.buffer(0)  # tentativa de "make valid"
        except Exception:
            pass
    return g


def _close_rings_inplace(coords):
    """
    Fecha anéis de Polygon/MultiPolygon (2D/3D/4D).
    Aceita anel no formato [[x,y(,z)(,m)], ...].
    """
    if not coords:
        return
    # Se for um único anel (lista de pontos) -> fecha
    if coords and isinstance(coords[0], (list, tuple)) and coords and \
       (isinstance(coords[0][0], (float, int)) or isinstance(coords[0][0], (list, tuple))):
        if coords and coords and isinstance(coords[0][0], (float, int)):
            ring = coords
            if len(ring) >= 1:
                first = ring[0]
                last = ring[-1]
                if first != last:
                    ring.append(first[:])  # fecha copiando o primeiro ponto
            return

    # Caso contrário, desce recursivamente (MultiPolygon -> vários anéis)
    for part in coords:
        _close_rings_inplace(part)


def _coerce_geojson_to_geometry(obj):
    """
    Converte geometry/feature/fc JSON OU WKT em GEOSGeometry (MultiPolygon) SRID 4326.
    Mantém Z/M se vierem nos dados.
    """
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith('{') or s.startswith('['):
            obj = json.loads(s)
        else:
            # WKT
            g = GEOSGeometry(s)
            if g.srid is None:
                g.srid = 4326
            if g.srid != 4326:
                g.transform(4326)
            if g.geom_type in ("Polygon", "MultiPolygon"):
                return MultiPolygon([g], srid=4326) if g.geom_type == "Polygon" else g
            return g.envelope

    if not isinstance(obj, dict):
        raise ValueError("Formato 'aoi' inválido.")

    t = (obj.get("type") or "").lower()
    if t == "feature":
        return _coerce_geojson_to_geometry(obj.get("geometry"))
    if t == "featurecollection":
        for feat in (obj.get("features") or []):
            try:
                g = _coerce_geojson_to_geometry(feat)
                if g.geom_type in ("Polygon", "MultiPolygon"):
                    return g
            except Exception:
                continue
        raise ValueError("FeatureCollection sem Polygon/MultiPolygon úteis.")
    if t in ("polygon", "multipolygon"):
        try:
            if t == "polygon":
                _close_rings_inplace(obj.get("coordinates"))
            else:
                for poly in (obj.get("coordinates") or []):
                    _close_rings_inplace(poly)
        except Exception:
            pass
        g = GEOSGeometry(json.dumps(obj))
        if g.srid is None:
            g.srid = 4326
        if g.srid != 4326:
            g.transform(4326)
        if not g.valid:
            try:
                g = g.buffer(0)
            except Exception:
                pass
        if g.geom_type == "Polygon":
            return MultiPolygon([g], srid=4326)
        if g.geom_type == "MultiPolygon":
            return g
        raise ValueError(f"Tipo inesperado após parse: {g.geom_type}")
    raise ValueError(f"Tipo de geometria não suportado: {obj.get('type')}")


def _annotate_clip_simplify(qs, geom_expr, tol):
    """
    Aplica MakeValid + SimplifyPreserveTopology no resultado do recorte.
    Mantém a dimensão original armazenada no banco (GEOS controla Z/M).
    """
    qs = qs.annotate(geom_valid=MakeValid(geom_expr)).annotate(
        geom_simpl=Func(
            F("geom_valid"),
            Value(float(tol)),
            function="ST_SimplifyPreserveTopology",
            output_field=GeometryField(srid=4326),
        )
    )
    return qs


# ============================ KML helpers (2D/3D/4D) ============================

def _split_xyz_m(pt: Iterable[float]) -> Tuple[Tuple[float, ...], Optional[float]]:
    """
    Recebe um ponto de dimensão 2/3/4 e retorna:
      - xyz: tupla (x,y) ou (x,y,z) para o KML (M é descartado no KML)
      - m: valor M (se houver), como float
    """
    # pt pode ser tuple/list de tamanho 2, 3 ou 4
    x = float(pt[0])
    y = float(pt[1])
    if len(pt) >= 3:
        z = float(pt[2])
        xyz = (x, y, z)
        m = float(pt[3]) if len(pt) >= 4 else None
        return xyz, m
    else:
        return (x, y), None


def _coords_for_kml_line(geom) -> Tuple[List[Tuple[float, ...]], List[float]]:
    """
    De um LineString/LinearRing retorna:
      - lista de coords para KML (XY ou XYZ)
      - lista de M se existir (XYZM)
    """
    coords_xyz = []
    m_vals = []
    for pt in geom.coords:
        xyz, m = _split_xyz_m(pt)
        coords_xyz.append(xyz)
        if m is not None:
            m_vals.append(m)
    return coords_xyz, m_vals


def _add_lines_to_kml(folder, geos_geom, line_color, name_prefix):
    """
    Adiciona LineStrings ao KML preservando Z quando existir e guardando M (se houver)
    em ExtendedData como JSON (campo 'm_values').
    """
    for ln in _extract_lines(geos_geom):
        coords_xyz, m_vals = _coords_for_kml_line(ln)
        if not coords_xyz:
            continue
        ls = folder.newlinestring(name=name_prefix, coords=coords_xyz)
        ls.style.linestyle.width = 2
        ls.style.linestyle.color = line_color
        if m_vals:
            try:
                # Armazena M como JSON; um viewer pode correlacionar por índice
                ls.extendeddata.newdata(
                    name="m_values", value=json.dumps(m_vals))
            except Exception:
                # Se ExtendedData falhar, seguimos em frente sem M
                pass


def _add_polygons_to_kml(folder, gj_geom, fill_color, line_color, name_prefix):
    """
    Adiciona Polygon/MultiPolygon ao KML preservando Z (se houver).
    M (4D) é descartado no KML e não é usual armazenar M por vértice em polígonos;
    se necessário um dia, poderíamos adicionar ExtendedData por anel, mas aqui omitimos.
    """
    def clean_ring(ring):
        out = []
        for pt in ring:
            xyz, _m = _split_xyz_m(pt)
            out.append(xyz)
        # garante fechado:
        if out and out[0] != out[-1]:
            out.append(out[0])
        return out

    def add_polygon(coords, nm):
        if not coords:
            return
        outer = clean_ring(coords[0])
        holes = [clean_ring(r) for r in coords[1:]] if len(coords) > 1 else []

        p = folder.newpolygon(name=nm)
        p.outerboundaryis = outer
        if holes:
            p.innerboundaryis = holes
        p.style.polystyle.color = fill_color
        p.style.linestyle.color = line_color
        p.style.linestyle.width = 2

    t = (gj_geom.get("type") or "").lower()
    if t == "polygon":
        add_polygon(gj_geom["coordinates"], name_prefix)
    elif t == "multipolygon":
        for i, coords in enumerate(gj_geom["coordinates"], 1):
            add_polygon(coords, f"{name_prefix} {i}")


def _color_a(alpha_int, rgb_hex_or_name):
    """Monta cor ARGB usando helper do simplekml."""
    from simplekml import Color
    base = getattr(Color, rgb_hex_or_name, Color.white)
    return Color.changealphaint(alpha_int, base)


def _extract_lines(geos_geom):
    """Retorna iterable de LineString somente com segmentos válidos."""
    lines = []
    if geos_geom.geom_type == "LineString":
        lines = [geos_geom]
    elif geos_geom.geom_type == "MultiLineString":
        lines = list(geos_geom)
    elif geos_geom.geom_type == "GeometryCollection":
        for sub in geos_geom:
            if sub.geom_type == "LineString":
                lines.append(sub)
            elif sub.geom_type == "MultiLineString":
                lines.extend(list(sub))
    return [ln for ln in lines if not ln.empty and len(ln.coords) >= 2]


def _overlay_palette(idx):
    """
    Define um par (fill_color, line_color) para cada overlay.
    Usa nomes suportados pelo simplekml.Color.
    """
    names = ["red", "orange", "yellow", "green",
             "cyan", "blue", "purple", "white"]
    name = names[idx % len(names)]
    fill = _color_a(40, name)  # ~16% opaco
    line = getattr(simplekml.Color, name, simplekml.Color.white)
    return fill, line


# ============================ Endpoint unificado ============================

@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def export_mapa_kmz(request):
    """
    POST /api/export/mapa/
    Body:
    {
      "aoi": <geometry|feature|featurecollection|WKT>,
      "layers": {
        "rios": true|false,
        "lt": true|false,
        "mf": true|false,
        "cidades": true|false,
        "limites_federais": true|false,
        "areas_estaduais": true|false
      },
      "uf": "SC",              # opcional (filtra Area.uf)
      "simplify": {            # opcional (defaults)
        "rios": 0.00002,
        "lt": 0.00002,
        "mf": 0.00002,
        "polygons": 0.00005
      },
      "format": "kml"|"kmz",   # default: kmz
      # FeatureCollection opcional (já-recortado no cliente)
      "overlays": {...},
      # FeatureCollection opcional (cru, recorta no servidor)
      "overlays_raw": {...}
    }
    - Suporta geometrias 2D/3D/4D (XYZ e XYZM). No KML são gravados XY ou XYZ.
      Se houver M, ele é anexado em ExtendedData (campo 'm_values') para linhas.
    """
    if simplekml is None:
        return Response({"detail": "simplekml não instalado."},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # valida payload básico
    ser = ExportMapaSerializer(data=request.data or {})
    ser.is_valid(raise_exception=True)
    data = ser.validated_data

    # AOI -> MultiPolygon SRID 4326 (mantém Z/M se vier)
    try:
        aoi = _coerce_geojson_to_geometry(data["aoi"])
        if not aoi or aoi.empty:
            return Response({"detail": "AOI vazia/inválida."}, status=400)
    except Exception as e:
        return Response({"detail": f"AOI inválida: {e}"}, status=400)

    # flags de camadas
    layers = (data.get("layers") or {})
    want_rios = bool(layers.get("rios"))
    want_lt = bool(layers.get("lt"))
    want_mf = bool(layers.get("mf"))
    want_cidades = bool(layers.get("cidades"))
    want_lim_fed = bool(layers.get("limites_federais"))
    want_areas_est = bool(layers.get("areas_estaduais"))

    uf = (data.get("uf") or "") or None

    # tolerâncias
    simp = data.get("simplify") or {}
    tol_rios = float(simp.get("rios", 0.00002))
    tol_lt = float(simp.get("lt",   0.00002))
    tol_mf = float(simp.get("mf",   0.00002))
    tol_pol = float(simp.get("polygons", 0.00005))

    out_format = (data.get("format") or "kmz").lower()

    # Overlays opcionais vindos do cliente
    overlays_raw_fc = (request.data or {}).get("overlays_raw") or {
        "type": "FeatureCollection", "features": []}
    overlays_fc = (request.data or {}).get("overlays") or {
        "type": "FeatureCollection", "features": []}

    # ---------- KML base ----------
    kml = simplekml.Kml()
    fld_aoi = kml.newfolder(name="AOI")
    fld_rios = None
    fld_lt = None
    fld_mf = None
    fld_cidades = None
    fld_fed = None
    fld_est = None

    # AOI (referência visual) – preserva Z se houver; M é descartado
    try:
        aoi_gj = json.loads(aoi.json)
        _add_polygons_to_kml(
            folder=fld_aoi,
            gj_geom=aoi_gj,
            fill_color=_color_a(60, "cyan"),
            line_color=simplekml.Color.cyan,
            name_prefix="AOI"
        )
    except Exception:
        pass

    total = 0

    # ---------- 1) Rios (linhas) ----------
    if want_rios:
        ids_qs = (
            Waterway.objects
            .filter(geom__intersects=aoi)
            .order_by('id')
            .values_list('id', flat=True)
        )
        for id_batch in _yield_ids_in_batches(ids_qs, batch_size=2000):
            try:
                qs = (
                    Waterway.objects
                    .filter(id__in=id_batch)
                    .annotate(clipped=Intersection("geom", Value(aoi, output_field=GeometryField(srid=4326))))
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_rios).only("id")
                for row in qs:
                    for ln in _extract_lines(row.geom_simpl):
                        coords_xyz, m_vals = _coords_for_kml_line(ln)
                        if not coords_xyz:
                            continue

                    if fld_rios is None:
                        fld_rios = kml.newfolder(name="Rios")

                        ls = fld_rios.newlinestring(coords=coords_xyz)
                        ls.style.linestyle.width = 2
                        ls.style.linestyle.color = simplekml.Color.royalblue
                        if m_vals:
                            try:
                                ls.extendeddata.newdata(
                                    name="m_values", value=json.dumps(m_vals))
                            except Exception:
                                pass
                        total += 1
            except OperationalError:
                _refresh_conn()
                qs = (
                    Waterway.objects
                    .filter(id__in=id_batch)
                    .annotate(clipped=Intersection("geom", Value(aoi, output_field=GeometryField(srid=4326))))
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_rios).only("id")
                for row in qs:
                    for ln in _extract_lines(row.geom_simpl):
                        coords_xyz, m_vals = _coords_for_kml_line(ln)
                        if not coords_xyz:
                            continue
                        ls = fld_rios.newlinestring(coords=coords_xyz)
                        ls.style.linestyle.width = 2
                        ls.style.linestyle.color = simplekml.Color.royalblue
                        if m_vals:
                            try:
                                ls.extendeddata.newdata(
                                    name="m_values", value=json.dumps(m_vals))
                            except Exception:
                                pass
                        total += 1

    # ---------- 2) Linhas de Transmissão (linhas) ----------
    if want_lt:
        ids_qs = (
            LinhaTransmissao.objects
            .filter(geom__intersects=aoi)
            .order_by('id')
            .values_list('id', flat=True)
        )
        for id_batch in _yield_ids_in_batches(ids_qs, batch_size=2000):
            try:
                qs = (
                    LinhaTransmissao.objects
                    .filter(id__in=id_batch)
                    .annotate(clipped=Intersection("geom", Value(aoi, output_field=GeometryField(srid=4326))))
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_lt).only("id")
                for row in qs:
                    for ln in _extract_lines(row.geom_simpl):
                        coords_xyz, m_vals = _coords_for_kml_line(ln)
                        if not coords_xyz:
                            continue

                        if fld_lt is None:
                            fld_lt = kml.newfolder(name="lt")

                        ls = fld_lt.newlinestring(coords=coords_xyz)
                        ls.style.linestyle.width = 2
                        ls.style.linestyle.color = simplekml.Color.red
                        if m_vals:
                            try:
                                ls.extendeddata.newdata(
                                    name="m_values", value=json.dumps(m_vals))
                            except Exception:
                                pass
                        total += 1
            except OperationalError:
                _refresh_conn()
                qs = (
                    LinhaTransmissao.objects
                    .filter(id__in=id_batch)
                    .annotate(clipped=Intersection("geom", Value(aoi, output_field=GeometryField(srid=4326))))
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_lt).only("id")
                for row in qs:
                    for ln in _extract_lines(row.geom_simpl):
                        coords_xyz, m_vals = _coords_for_kml_line(ln)
                        if not coords_xyz:
                            continue
                        ls = fld_lt.newlinestring(coords=coords_xyz)
                        ls.style.linestyle.width = 2
                        ls.style.linestyle.color = simplekml.Color.red
                        if m_vals:
                            try:
                                ls.extendeddata.newdata(
                                    name="m_values", value=json.dumps(m_vals))
                            except Exception:
                                pass
                        total += 1

    # ---------- 3) Malha Ferroviária (linhas) ----------
    try:
        fld_mf
    except NameError:
        fld_mf = kml.newfolder(name="Malha Ferroviária")

    if want_mf:
        ids_qs = (
            MalhaFerroviaria.objects
            .filter(geom__intersects=aoi)
            .order_by('id')
            .values_list('id', flat=True)
        )

        _some = list(ids_qs[:1])
        if not _some:
            print("[export_mapa_kmz] MF: 0 features intersectando AOI")

        batch_total = 0
        for id_batch in _yield_ids_in_batches(ids_qs, batch_size=2000):
            try:
                qs = (
                    MalhaFerroviaria.objects
                    .filter(id__in=id_batch)
                    .annotate(
                        clipped=Intersection("geom", Value(
                            aoi, output_field=GeometryField(srid=4326)))
                    )
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_mf).only("id")

                for row in qs:
                    for ln in _extract_lines(row.geom_simpl):
                        coords_xyz, m_vals = _coords_for_kml_line(ln)
                        if not coords_xyz:
                            continue
                        if fld_mf is None:
                            fld_mf = kml.newfolder(name="mf")

                        ls = fld_mf.newlinestring(coords=coords_xyz)
                        ls.style.linestyle.width = 2
                        ls.style.linestyle.color = simplekml.Color.black
                        if m_vals:
                            try:
                                ls.extendeddata.newdata(
                                    name="m_values", value=json.dumps(m_vals))
                            except Exception:
                                pass
                        total += 1
                        batch_total += 1

            except OperationalError:
                _refresh_conn()
                qs = (
                    MalhaFerroviaria.objects
                    .filter(id__in=id_batch)
                    .annotate(
                        clipped=Intersection("geom", Value(
                            aoi, output_field=GeometryField(srid=4326)))
                    )
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_mf).only("id")
                for row in qs:
                    for ln in _extract_lines(row.geom_simpl):
                        coords_xyz, m_vals = _coords_for_kml_line(ln)
                        if not coords_xyz:
                            continue
                        ls = fld_mf.newlinestring(coords=coords_xyz)
                        ls.style.linestyle.width = 2
                        ls.style.linestyle.color = simplekml.Color.black
                        if m_vals:
                            try:
                                ls.extendeddata.newdata(
                                    name="m_values", value=json.dumps(m_vals))
                            except Exception:
                                pass
                        total += 1
                        batch_total += 1

        print(f"[export_mapa_kmz] MF: adicionadas {batch_total} linhas ao KML")

    # ---------- 4) Cidades (polígonos) ----------
    if want_cidades:
        ids_qs = (
            Cidade.objects
            .filter(geom__intersects=aoi)
            .order_by('id')
            .values_list('id', flat=True)
        )
        for id_batch in _yield_ids_in_batches(ids_qs, batch_size=1000):
            try:
                qs = (
                    Cidade.objects
                    .filter(id__in=id_batch)
                    .annotate(clipped=Intersection("geom", Value(aoi, output_field=GeometryField(srid=4326))))
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_pol).only("id")
                for row in qs:
                    gj = json.loads(row.geom_simpl.json)
                    if fld_cidades is None:
                        fld_cidades = kml.newfolder(name="cidades")
                    _add_polygons_to_kml(
                        folder=fld_cidades,
                        gj_geom=gj,
                        fill_color=_color_a(50, "yellow"),
                        line_color=simplekml.Color.yellow,
                        name_prefix="Município"
                    )
                    total += 1
            except OperationalError:
                _refresh_conn()
                qs = (
                    Cidade.objects
                    .filter(id__in=id_batch)
                    .annotate(clipped=Intersection("geom", Value(aoi, output_field=GeometryField(srid=4326))))
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_pol).only("id")
                for row in qs:
                    gj = json.loads(row.geom_simpl.json)
                    _add_polygons_to_kml(
                        folder=fld_cidades,
                        gj_geom=gj,
                        fill_color=_color_a(50, "yellow"),
                        line_color=simplekml.Color.yellow,
                        name_prefix="Município"
                    )
                    total += 1

    # ---------- 5) Áreas Federais (polígonos) ----------
    if want_lim_fed:
        ids_qs = (
            LimiteFederal.objects
            .filter(geom__intersects=aoi)
            .order_by('id')
            .values_list('id', flat=True)
        )
        for id_batch in _yield_ids_in_batches(ids_qs, batch_size=1000):
            try:
                qs = (
                    LimiteFederal.objects
                    .filter(id__in=id_batch)
                    .annotate(clipped=Intersection("geom", Value(aoi, output_field=GeometryField(srid=4326))))
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_pol).only("id")
                for row in qs:
                    gj = json.loads(row.geom_simpl.json)
                    if fld_fed is None:
                        fld_fed = kml.newfolder(name="federais")
                    _add_polygons_to_kml(
                        folder=fld_fed,
                        gj_geom=gj,
                        fill_color=_color_a(60, "green"),
                        line_color=simplekml.Color.green,
                        name_prefix="Área Federal"
                    )
                    total += 1
            except OperationalError:
                _refresh_conn()
                qs = (
                    LimiteFederal.objects
                    .filter(id__in=id_batch)
                    .annotate(clipped=Intersection("geom", Value(aoi, output_field=GeometryField(srid=4326))))
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_pol).only("id")
                for row in qs:
                    gj = json.loads(row.geom_simpl.json)
                    _add_polygons_to_kml(
                        folder=fld_fed,
                        gj_geom=gj,
                        fill_color=_color_a(60, "green"),
                        line_color=simplekml.Color.green,
                        name_prefix="Área Federal"
                    )
                    total += 1

    # ---------- 6) Áreas Estaduais (polígonos) ----------
    if want_areas_est:
        base_qs = Area.objects.filter(geom__intersects=aoi)
        if uf:
            base_qs = base_qs.filter(uf=uf)
        ids_qs = base_qs.order_by('id').values_list('id', flat=True)
        for id_batch in _yield_ids_in_batches(ids_qs, batch_size=1000):
            try:
                qs = (
                    Area.objects
                    .filter(id__in=id_batch)
                    .annotate(clipped=Intersection("geom", Value(aoi, output_field=GeometryField(srid=4326))))
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_pol).only("id")
                for row in qs:
                    if fld_est is None:
                        fld_est = kml.newfolder(name="estaduais")
                    gj = json.loads(row.geom_simpl.json)
                    _add_polygons_to_kml(
                        folder=fld_est,
                        gj_geom=gj,
                        fill_color=_color_a(60, "purple"),
                        line_color=simplekml.Color.purple,
                        name_prefix="Área Estadual"
                    )
                    total += 1
            except OperationalError:
                _refresh_conn()
                qs = (
                    Area.objects
                    .filter(id__in=id_batch)
                    .annotate(clipped=Intersection("geom", Value(aoi, output_field=GeometryField(srid=4326))))
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_pol).only("id")
                for row in qs:
                    gj = json.loads(row.geom_simpl.json)
                    _add_polygons_to_kml(
                        folder=fld_est,
                        gj_geom=gj,
                        fill_color=_color_a(60, "purple"),
                        line_color=simplekml.Color.purple,
                        name_prefix="Área Estadual"
                    )
                    total += 1

    # ---------- 7) Overlays (KMLs Secundários) ----------
    # Decide fonte: se houver algo cru, recorta no servidor; senão usa o já-recortado do cliente
    src_fc = overlays_raw_fc if (
        (overlays_raw_fc.get("features") or [])) else overlays_fc

    if src_fc and isinstance(src_fc, dict) and src_fc.get("features"):
        fld_over = kml.newfolder(name="Overlays (KMLs Secundários)")
        feats = src_fc.get("features") or []

        def _line_tol_for(overlay_id: str) -> float:
            oid = (overlay_id or "").strip().lower()
            if oid in {"mf", "ferrovia", "ferrovias", "malha_ferroviaria"}:
                return tol_mf
            return tol_lt

        id_to_color = {}
        id_to_folder = {}

        for i, feat in enumerate(feats):
            try:
                props = (feat.get("properties") or {})
                overlay_id = props.get("__overlay_id") or f"overlay_{i+1}"
                if overlay_id not in id_to_color:
                    idx = len(id_to_color)
                    id_to_color[overlay_id] = _overlay_palette(idx)
                fill_color, line_color = id_to_color[overlay_id]

                # NOVO: subpasta por overlay (nome amigável vindo do front)
                if overlay_id not in id_to_folder:
                    id_to_folder[overlay_id] = fld_over.newfolder(
                        name=str(overlay_id))
                subfolder = id_to_folder[overlay_id]

                g = _safe_geos_from_geojson(feat.get("geometry"))
                if not g or g.empty:
                    continue

                g_clip = g.intersection(aoi)
                if not g_clip or g_clip.empty:
                    continue

                if g_clip.geom_type in ("Polygon", "MultiPolygon"):
                    try:
                        g_clip = g_clip.simplify(
                            tol_pol, preserve_topology=True)
                    except Exception:
                        pass
                    if g_clip.empty:
                        continue
                    gj = json.loads(g_clip.json)
                    _add_polygons_to_kml(folder=subfolder, gj_geom=gj, fill_color=fill_color,
                                         line_color=line_color, name_prefix=str(overlay_id))

                    total += 1

                elif g_clip.geom_type in ("LineString", "MultiLineString", "GeometryCollection"):
                    try:
                        line_tol = _line_tol_for(overlay_id)
                        g_clip_simpl = g_clip.simplify(
                            line_tol, preserve_topology=True)
                    except Exception:
                        g_clip_simpl = g_clip
                    _add_lines_to_kml(folder=subfolder, geos_geom=g_clip_simpl,
                                      line_color=line_color, name_prefix=str(overlay_id))

                    total += 1

                # ignora Point/MultiPoint
            except Exception:
                # não derruba a exportação por 1 feature ruim
                continue

    # Se nenhuma feição (além da AOI) foi encontrada e alguma camada foi pedida → 204
    if total == 0 and (want_rios or want_lt or want_mf or want_cidades or want_lim_fed or want_areas_est):
        return Response(status=204)

    # ---------- Saída ----------
    if out_format == "kml":
        kml_xml = kml.kml()
        resp = HttpResponse(
            kml_xml,
            content_type="application/vnd.google-earth.kml+xml; charset=utf-8"
        )
        resp["Content-Disposition"] = 'attachment; filename="mapa_recorte.kml"'
        return resp

    # KMZ padrão (mimetype + doc.kml)
    kml_xml_bytes = kml.kml().encode("utf-8")
    kmz_bytes = io.BytesIO()
    with zipfile.ZipFile(kmz_bytes, mode="w") as zf:
        # 1) mimetype primeiro, sem compressão
        zinfo = zipfile.ZipInfo("mimetype")
        zinfo.compress_type = zipfile.ZIP_STORED
        zf.writestr(zinfo, b"application/vnd.google-earth.kmz")
        # 2) doc.kml (com compressão)
        zf.writestr("doc.kml", kml_xml_bytes,
                    compress_type=zipfile.ZIP_DEFLATED)

    kmz_bytes.seek(0)
    resp = HttpResponse(
        kmz_bytes.read(), content_type="application/vnd.google-earth.kmz")
    resp["Content-Disposition"] = 'attachment; filename="mapa_recorte.kmz"'
    return resp
