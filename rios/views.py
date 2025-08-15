# rios/views.py
import io
import json
import zipfile

from django.contrib.gis.db.models import GeometryField
from django.contrib.gis.db.models.functions import (AsGeoJSON, Intersection,
                                                    MakeValid)
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon
from django.contrib.gis.geos import Polygon
from django.contrib.gis.geos import Polygon as GEOSPolygon
from django.db.models import F, Func, Value
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import Waterway

try:
    import simplekml
except ImportError:
    simplekml = None


# ---------------------------
# Helpers
# ---------------------------

def _close_rings_inplace(coords):
    """
    Fecha anéis de Polygon/MultiPolygon (lista de rings) in-place.
    Funciona para:
      - Polygon: coords = [ ring_externo, ring_interno?, ... ]
      - MultiPolygon: coords = [ [rings_do_poly1], [rings_do_poly2], ... ]
    """
    if not coords:
        return
    # Polygon ring simples?
    if isinstance(coords[0][0], (float, int)):
        ring = coords
        if len(ring) >= 1:
            fx, fy = ring[0][:2]
            lx, ly = ring[-1][:2]
            if fx != lx or fy != ly:
                ring.append(ring[0])
    else:
        # lista de rings (Polygon) ou lista de polígonos (MultiPolygon)
        for part in coords:
            _close_rings_inplace(part)


def _coerce_geojson_to_geometry(obj):
    """
    Aceita em 'obj':
      - dict geometry {type: Polygon/MultiPolygon, coordinates: ...}
      - dict Feature {type: 'Feature', geometry: {...}}
      - dict FeatureCollection {type: 'FeatureCollection', features: [...]}
      - str JSON com qualquer um dos acima
      - str WKT (POLYGON/MULTIPOLYGON)
    Retorna GEOSGeometry (MultiPolygon) com SRID 4326 sempre que possível.
    """
    # Se for string, tente JSON; se não for JSON, tenta WKT
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith('{') or s.startswith('['):
            try:
                obj = json.loads(s)
            except Exception as e:
                raise ValueError(f"JSON inválido em 'aoi': {e}")
        else:
            # tenta WKT
            try:
                g = GEOSGeometry(s)
                if g.srid is None:
                    g.srid = 4326
                if g.srid != 4326:
                    g.transform(4326)
                if g.geom_type in ("Polygon", "MultiPolygon"):
                    return MultiPolygon([g], srid=4326) if isinstance(g, Polygon) else g
                return g.envelope
            except Exception as e:
                raise ValueError(f"WKT inválido em 'aoi': {e}")

    if not isinstance(obj, dict):
        raise ValueError("Formato de 'aoi' não reconhecido.")

    t = (obj.get("type") or "").lower()

    # Feature
    if t == "feature":
        geom = obj.get("geometry")
        if not geom:
            raise ValueError("Feature sem 'geometry'.")
        return _coerce_geojson_to_geometry(geom)

    # FeatureCollection -> usa o primeiro Polygon/MultiPolygon
    if t == "featurecollection":
        feats = obj.get("features") or []
        for feat in feats:
            try:
                g = _coerce_geojson_to_geometry(feat)
                if g.geom_type in ("Polygon", "MultiPolygon"):
                    return g
            except Exception:
                continue
        raise ValueError(
            "FeatureCollection sem Polygon/MultiPolygon utilizáveis.")

    # Geometry
    if t in ("polygon", "multipolygon"):
        # Fecha anéis para evitar invalid ring orientation/closure
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
                g = g.buffer(0)  # MakeValid
            except Exception:
                pass

        if isinstance(g, Polygon):
            return MultiPolygon([g], srid=4326)
        if isinstance(g, MultiPolygon):
            return g
        raise ValueError(f"Tipo inesperado após parse: {g.geom_type}")

    raise ValueError(f"Tipo de geometria não suportado: {obj.get('type')}")


def _parse_bbox(bbox_str):
    """
    bbox_str = 'minx,miny,maxx,maxy' (WGS84)
    Retorna GEOSPolygon (SRID 4326) representando o envelope.
    """
    try:
        minx, miny, maxx, maxy = [float(x) for x in bbox_str.split(",")]
        ring = [(minx, miny), (maxx, miny), (maxx, maxy),
                (minx, maxy), (minx, miny)]
        return GEOSPolygon(ring, srid=4326)
    except Exception:
        return None


def _parse_aoi(aoi_str):
    """
    Tenta ler aoi a partir de uma string JSON (geometry/feature/fc) ou WKT.
    """
    try:
        return _coerce_geojson_to_geometry(aoi_str)
    except Exception:
        return None


# ---------------------------
# API: /api/rios/geojson/
# ---------------------------

@api_view(["GET"])
@permission_classes([AllowAny])
def rios_geojson(request):
    """
    GET /api/rios/geojson/?bbox=minx,miny,maxx,maxy&simplify=0.00002&limit=5000
    ou
    GET /api/rios/geojson/?aoi=<GeoJSON Polygon/MultiPolygon ou WKT>&simplify=0.00002&limit=5000

    Retorna FeatureCollection com rios (clipped se houver AOI) e simplificados.
    """
    bbox_str = request.GET.get("bbox")
    aoi_str = request.GET.get("aoi")
    simplify_tol = float(request.GET.get("simplify") or 0.00002)
    limit = int(request.GET.get("limit") or 5000)

    aoi = None
    if bbox_str:
        aoi = _parse_bbox(bbox_str)
        if aoi is None:
            return JsonResponse({"detail": "bbox inválido."}, status=400)
    elif aoi_str:
        aoi = _parse_aoi(aoi_str)
        if aoi is None:
            return JsonResponse({"detail": "aoi inválido."}, status=400)

    qs = Waterway.objects.all()

    if aoi is not None:
        qs = qs.filter(geom__intersects=aoi).annotate(
            clipped=Intersection("geom", Value(
                aoi, output_field=GeometryField(srid=4326)))
        )
        geom_expr = F("clipped")
    else:
        geom_expr = F("geom")

    # MakeValid + ST_SimplifyPreserveTopology
    qs = (
        qs.annotate(geom_valid=MakeValid(geom_expr))
          .annotate(
              geom_simpl=Func(
                  F("geom_valid"),
                  Value(simplify_tol),
                  function="ST_SimplifyPreserveTopology",
                  output_field=GeometryField(srid=4326),
              )
        )
        .annotate(geojson=AsGeoJSON("geom_simpl"))
        .values("id", "name", "source", "geojson")[:max(1, limit)]
    )

    features = []
    for row in qs:
        try:
            geom = json.loads(row["geojson"])
        except Exception:
            continue
        # só exporta linhas
        if geom.get("type") not in ("LineString", "MultiLineString", "GeometryCollection"):
            continue
        features.append({
            "type": "Feature",
            "id": row["id"],
            "properties": {
                "name": row.get("name"),
                "source": row.get("source"),
            },
            "geometry": geom,
        })

    return JsonResponse({"type": "FeatureCollection", "features": features}, status=200)


# ---------------------------
# API: /api/export/rios/
# ---------------------------

@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def export_rios_kmz(request):
    """
    POST /api/export/rios/
    body: { "aoi": <geometry|feature|featurecollection|string JSON|WKT> }

    Gera KMZ (ou KML com ?format=kml) contendo:
      - Folder "AOI": polígono(s) da área de interesse (desenhado/importado)
      - Folder "Rios": trechos de rios estritamente DENTRO da AOI
    """
    if simplekml is None:
        return Response({"detail": "simplekml não instalado."},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    data = request.data or {}
    if "aoi" not in data:
        # também aceita querystring (?aoi=...)
        aoi_param = request.GET.get("aoi")
        if aoi_param:
            data["aoi"] = aoi_param

    aoi_in = data.get("aoi")
    if not aoi_in:
        return Response({"detail": "Campo 'aoi' obrigatório."}, status=400)

    try:
        # -> MultiPolygon/Polygon em EPSG:4326
        aoi = _coerce_geojson_to_geometry(aoi_in)
        if not aoi or aoi.empty:
            return Response({"detail": "AOI vazia/inválida."}, status=400)
    except Exception as e:
        return Response({"detail": f"AOI inválida: {e}"}, status=400)

    # ---------- Monta KML com duas pastas ----------
    kml = simplekml.Kml()
    fld_aoi = kml.newfolder(name="AOI")
    fld_rios = kml.newfolder(name="Rios")

    # Adiciona AOI (Polygon/MultiPolygon) como placemarks
    try:
        aoi_gj = json.loads(aoi.json)  # GeoJSON do AOI

        def add_polygon(coords, name="AOI"):
            """coords = [outer, hole1?, hole2?, ...] (em lon,lat)"""
            if not coords:
                return
            outer = [(float(x), float(y)) for (x, y) in coords[0]]
            holes = []
            for ring in coords[1:]:
                holes.append([(float(x), float(y)) for (x, y) in ring])

            p = fld_aoi.newpolygon(name=name)
            p.outerboundaryis = outer
            if holes:
                p.innerboundaryis = holes
            # estilo
            p.style.polystyle.color = simplekml.Color.changealphaint(
                80, simplekml.Color.cyan)  # ~31% opacidade
            p.style.linestyle.color = simplekml.Color.cyan
            p.style.linestyle.width = 2

        if aoi_gj.get("type") == "Polygon":
            add_polygon(aoi_gj["coordinates"], "AOI")
        elif aoi_gj.get("type") == "MultiPolygon":
            for idx, coords in enumerate(aoi_gj["coordinates"], start=1):
                # cada item é [outer, holes...]
                add_polygon(coords, f"AOI {idx}")
        else:
            # fallback (não deve ocorrer, pois já normalizamos)
            pass
    except Exception:
        # se der algo errado, apenas segue com os rios
        pass

    # ---------- Consulta rios recortados ----------
    qs = (
        Waterway.objects
        .filter(geom__intersects=aoi))
    qs = qs.annotate(
        clipped=Intersection('geom', Value(
            aoi, output_field=GeometryField(srid=4326)))
    ).annotate(
        clipped_valid=MakeValid(F('clipped'))
    ).annotate(
        clipped_simpl=Func(
            F('clipped_valid'),
            Value(0.00002),  # ajuste se quiser mais/menos detalhado
            function='ST_SimplifyPreserveTopology',
            output_field=GeometryField(srid=4326),
        )
    ).only('id')

    # Adiciona rios (linhas) na pasta "Rios"
    total_rios = 0
    for row in qs.iterator(chunk_size=1000):
        try:
            g = row.clipped_simpl  # GEOSGeometry
            if g.empty:
                continue

            # extrai apenas linhas
            lines = []
            if g.geom_type == "LineString":
                lines = [g]
            elif g.geom_type == "MultiLineString":
                lines = list(g)
            elif g.geom_type == "GeometryCollection":
                for sub in g:
                    if sub.geom_type == "LineString":
                        lines.append(sub)
                    elif sub.geom_type == "MultiLineString":
                        lines.extend(list(sub))
            else:
                continue

            for ln in lines:
                if ln.empty:
                    continue
                coords = [(x, y) for (x, y) in ln.coords]
                if len(coords) < 2:
                    continue
                ls = fld_rios.newlinestring(coords=coords)
                ls.style.linestyle.width = 2
                ls.style.linestyle.color = simplekml.Color.royalblue
                total_rios += 1
        except Exception:
            continue

    # ---------- Saída (KML/KMZ) ----------
    # OBS: Mantive 204 quando não há rios (mesmo incluindo AOI). Se quiser SEMPRE gerar arquivo,
    #      remova o bloco abaixo.
    if total_rios == 0:
        return Response(status=204)

    # Se pedir KML puro, devolve KML (QGIS abre sempre)
    if request.GET.get('format', '').lower() == 'kml':
        kml_xml = kml.kml()
        resp = HttpResponse(
            kml_xml,
            content_type="application/vnd.google-earth.kml+xml; charset=utf-8"
        )
        resp["Content-Disposition"] = 'attachment; filename="rios_recorte.kml"'
        return resp

    # KMZ compatível com QGIS/GE: 'mimetype' + 'doc.kml' na raiz
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
    resp["Content-Disposition"] = 'attachment; filename="rios_recorte.kmz"'
    return resp
