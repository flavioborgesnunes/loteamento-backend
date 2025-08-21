# rios/views.py (adicione os imports que faltarem)
import io
import json
import zipfile
from typing import Iterable

from django.contrib.gis.db.models import GeometryField
from django.contrib.gis.db.models.functions import (AsGeoJSON, Intersection,
                                                    MakeValid)
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon
from django.contrib.gis.geos import Polygon as GEOSPolygon
from django.db.models import F, Func, Value
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from geodata.models import (Area, Cidade,  # ajuste o app se necessário
                            LimiteFederal, LinhaTransmissao)
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rios.models import Waterway  # rios

from .serializers import ExportMapaSerializer

try:
    import simplekml
except ImportError:
    simplekml = None


# ============================ Helpers comuns ============================

from django.db import connection
from django.db.utils import OperationalError


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


def _refresh_conn():
    """
    Fecha a conexão se estiver inválida/obsoleta para forçar reabertura limpa.
    """
    try:
        connection.close_if_unusable_or_obsolete()
    except Exception:
        # Em último caso, força fechar
        try:
            connection.close()
        except Exception:
            pass


def _close_rings_inplace(coords):
    if not coords:
        return
    if isinstance(coords[0][0], (float, int)):
        ring = coords
        if len(ring) >= 1:
            fx, fy = ring[0][:2]
            lx, ly = ring[-1][:2]
            if fx != lx or fy != ly:
                ring.append(ring[0])
    else:
        for part in coords:
            _close_rings_inplace(part)


def _coerce_geojson_to_geometry(obj):
    """
    Converte geometry/feature/fc JSON OU WKT em GEOSGeometry (MultiPolygon) SRID 4326.
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


def _add_polygons_to_kml(folder, gj_geom, fill_color, line_color, name_prefix):
    """
    gj_geom: GeoJSON-like dict de Polygon ou MultiPolygon
    """
    def add_polygon(coords, nm):
        if not coords:
            return
        outer = [(float(x), float(y)) for (x, y) in coords[0]]
        holes = []
        for ring in coords[1:]:
            holes.append([(float(x), float(y)) for (x, y) in ring])

        p = folder.newpolygon(name=nm)
        p.outerboundaryis = outer
        if holes:
            p.innerboundaryis = holes
        p.style.polystyle.color = fill_color
        p.style.linestyle.color = line_color
        p.style.linestyle.width = 2

    t = gj_geom.get("type")
    if t == "Polygon":
        add_polygon(gj_geom["coordinates"], name_prefix)
    elif t == "MultiPolygon":
        for i, coords in enumerate(gj_geom["coordinates"], 1):
            add_polygon(coords, f"{name_prefix} {i}")


def _color_a(alpha_int, rgb_hex):
    """ simplekml: monta cor ARGB usando helper """
    from simplekml import Color

    # Color no simplekml é AABBGGRR; usaremos helper changealphaint com uma base
    base = getattr(Color, rgb_hex, Color.white)
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


def _annotate_clip_simplify(qs, geom_expr, tol):
    qs = qs.annotate(geom_valid=MakeValid(geom_expr)).annotate(
        geom_simpl=Func(
            F("geom_valid"),
            Value(float(tol)),
            function="ST_SimplifyPreserveTopology",
            output_field=GeometryField(srid=4326),
        )
    )
    return qs

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
        "cidades": true|false,
        "limites_federais": true|false,
        "areas_estaduais": true|false
      },
      "uf": "SC",              # opcional (aplica em Area.uf; Cidade não tem UF na model fornecida)
      "simplify": {            # opcional (defaults)
        "rios": 0.00002,
        "lt": 0.00002,
        "polygons": 0.00005
      },
      "format": "kml"|"kmz"    # default: kmz
    }
    """
    if simplekml is None:
        return Response({"detail": "simplekml não instalado."},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    ser = ExportMapaSerializer(data=request.data or {})
    ser.is_valid(raise_exception=True)
    data = ser.validated_data

    # AOI -> MultiPolygon SRID 4326
    try:
        aoi = _coerce_geojson_to_geometry(data["aoi"])
        if not aoi or aoi.empty:
            return Response({"detail": "AOI vazia/inválida."}, status=400)
    except Exception as e:
        return Response({"detail": f"AOI inválida: {e}"}, status=400)

    layers = (data.get("layers") or {})
    want_rios = bool(layers.get("rios"))
    want_lt = bool(layers.get("lt"))
    want_cidades = bool(layers.get("cidades"))
    want_lim_fed = bool(layers.get("limites_federais"))
    want_areas_est = bool(layers.get("areas_estaduais"))

    uf = (data.get("uf") or "") or None

    simp = data.get("simplify") or {}
    tol_rios = float(simp.get("rios", 0.00002))
    tol_lt = float(simp.get("lt", 0.00002))
    tol_pol = float(simp.get("polygons", 0.00005))

    out_format = (data.get("format") or "kmz").lower()

    # ---------- KML base ----------
    kml = simplekml.Kml()
    fld_aoi = kml.newfolder(name="AOI")
    fld_rios = kml.newfolder(name="Rios")
    fld_lt = kml.newfolder(name="Linhas de Transmissão")
    fld_cidades = kml.newfolder(name="Cidades")
    fld_fed = kml.newfolder(name="Áreas Federais")
    fld_est = kml.newfolder(name="Áreas Estaduais")

    # AOI (apenas para referência visual)
    try:
        aoi_gj = json.loads(aoi.json)
        _add_polygons_to_kml(
            folder=fld_aoi,
            gj_geom=aoi_gj,
            fill_color=_color_a(60, "cyan"),     # ~24% opaco
            line_color=simplekml.Color.cyan,
            name_prefix="AOI"
        )
    except Exception:
        pass

    total = 0

   # ---------- 1) Rios (linhas) ----------
    if want_rios:
        # 1a. Pegue IDs que intersectam (barato)
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
                        coords = [(x, y) for (x, y) in ln.coords]
                        ls = fld_rios.newlinestring(coords=coords)
                        ls.style.linestyle.width = 2
                        ls.style.linestyle.color = simplekml.Color.royalblue
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
                        coords = [(x, y) for (x, y) in ln.coords]
                        ls = fld_rios.newlinestring(coords=coords)
                        ls.style.linestyle.width = 2
                        ls.style.linestyle.color = simplekml.Color.royalblue
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
                        coords = [(x, y) for (x, y) in ln.coords]
                        ls = fld_lt.newlinestring(coords=coords)
                        ls.style.linestyle.width = 2
                        ls.style.linestyle.color = simplekml.Color.red
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
                        coords = [(x, y) for (x, y) in ln.coords]
                        ls = fld_lt.newlinestring(coords=coords)
                        ls.style.linestyle.width = 2
                        ls.style.linestyle.color = simplekml.Color.red
                        total += 1

    # ---------- 3) Cidades (polígonos) ----------
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

    # ---------- 4) Áreas Federais (polígonos) ----------
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
                    _add_polygons_to_kml(
                        folder=fld_fed,
                        gj_geom=gj,
                        fill_color=_color_a(60, "black"),
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

    # ---------- 5) Áreas Estaduais (polígonos) ----------
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

    # Se nenhuma feição (além da AOI) foi encontrada e alguma camada foi pedida → 204
    if total == 0 and (want_rios or want_lt or want_cidades or want_lim_fed or want_areas_est):
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
