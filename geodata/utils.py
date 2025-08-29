# projetos/utils.py
from __future__ import annotations

import io
import json
import zipfile
from typing import Dict, Iterable, List, Tuple

from django.contrib.gis.db.models.functions import Intersection, MakeValid
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon
from django.db.models import F
from django.utils.text import slugify
# MODELS PostGIS (ajuste os imports conforme seus apps)
from geodata.models import (Area, Cidade, LimiteFederal, LinhaTransmissao,
                            MalhaFerroviaria)
from projetos.models import ProjectFeature
from rios.models import Waterway

try:
    import simplekml
except Exception:  # pragma: no cover
    simplekml = None


def _mp(g: GEOSGeometry) -> MultiPolygon:
    if g.geom_type == "Polygon":
        return MultiPolygon([g], srid=g.srid or 4326)
    if g.geom_type != "MultiPolygon":
        raise ValueError("AOI deve ser Polygon/MultiPolygon.")
    return g


def _norm_geom(obj) -> GEOSGeometry:
    g = GEOSGeometry(json.dumps(obj) if isinstance(obj, dict) else obj)
    if g.srid is None:
        g.srid = 4326
    return g


def _folder(kml, name: str):
    return kml.newfolder(name=name)


def _put_lines(folder, geos: GEOSGeometry, name=""):
    def _one(ls):
        folder.newlinestring(name=name or "Linha", coords=list(ls.coords))
    if geos.geom_type == "LineString":
        _one(geos)
    elif geos.geom_type == "MultiLineString":
        for ls in geos:
            _one(ls)


def _put_polys(folder, geos: GEOSGeometry, name=""):
    def _one(poly):
        for ring in poly:
            folder.newpolygon(name=name or "Polígono",
                              outerboundaryis=list(ring.coords))
    if geos.geom_type == "Polygon":
        _one(geos)
    elif geos.geom_type == "MultiPolygon":
        for poly in geos:
            _one(poly)


def _put_points(folder, geos: GEOSGeometry, name=""):
    def _one(pt):
        folder.newpoint(name=name or "Ponto", coords=[(pt.x, pt.y)])
    if geos.geom_type == "Point":
        _one(geos)
    elif geos.geom_type == "MultiPoint":
        for pt in geos:
            _one(pt)


def _put_geom(folder, geos: GEOSGeometry, name=""):
    if geos.geom_type in ("LineString", "MultiLineString"):
        _put_lines(folder, geos, name)
    elif geos.geom_type in ("Polygon", "MultiPolygon"):
        _put_polys(folder, geos, name)
    elif geos.geom_type in ("Point", "MultiPoint"):
        _put_points(folder, geos, name)


def _simple_or_same(g: GEOSGeometry, tol: float | None) -> GEOSGeometry:
    if tol and tol > 0:
        try:
            return g.simplify(tol, preserve_topology=True)
        except Exception:
            return g
    return g


def _query_and_draw(
    folder,
    qs,
    aoi_mp: MultiPolygon,
    tol: float | None,
    name_field: str | None = None,
    default_name: str = "",
):
    """
    Recorta queryset por AOI usando GEOS (em Python) — simples e robusto.
    Para grandes volumes, troque por annotate(Intersection(...)) em SQL.
    """
    count = 0
    for row in qs.iterator():
        g = getattr(row, "geom") or getattr(row, "geometry", None)
        if not g:
            continue
        if not g.valid:
            g = g.buffer(0)
        inter = g.intersection(aoi_mp)
        if inter.empty:
            continue
        inter = _simple_or_same(inter, tol)
        nm = getattr(row, name_field) if name_field else None
        _put_geom(folder, inter, str(nm or default_name))
        count += 1
    return count


def build_kmz_from_payload(
    *,
    project,                          # Project (para buscar overlays salvos)
    aoi_geojson: dict,
    layer_flags: Dict,
    simplify: Dict | None = None,
    include_saved_overlays: bool = True,
    out_format: str = "kmz",
) -> Tuple[bytes, str, str]:
    """
    Gera KML/KMZ com:
      - AOI
      - Overlays Secundários (ProjectFeature) agrupados por overlay_id
      - Camadas PostGIS conforme flags:
          rios, lt, mf, cidades, limites_federais, areas_estaduais
    """
    if simplekml is None:
        raise RuntimeError("simplekml não instalado. pip install simplekml")

    simplify = simplify or {}
    tol_lines = float(simplify.get("lines", simplify.get(
        "rios", simplify.get("lt", 0))) or 0) or None
    tol_polys = float(simplify.get(
        "polygons", simplify.get("polygon", 0)) or 0) or None

    aoi = _mp(_norm_geom(aoi_geojson))

    kml = simplekml.Kml()

    # 1) AOI
    f_aoi = _folder(kml, "AOI")
    for poly in aoi:
        for ring in poly:
            f_aoi.newpolygon(name="AOI", outerboundaryis=list(ring.coords))

    # 2) Overlays Secundários (salvos em ProjectFeature)
    if include_saved_overlays:
        f_ov = _folder(kml, "Overlays Secundários")
        from projetos.models import \
            ProjectFeature  # import local para evitar ciclos

        # agrupado por overlay_id
        feats = (
            ProjectFeature.objects
            .filter(project=project)
            .only("overlay_id", "geom", "properties", "color")
        )

        groups: Dict[str, List[ProjectFeature]] = {}
        for pf in feats.iterator():
            groups.setdefault(pf.overlay_id or "overlay", []).append(pf)

        for overlay_id, items in groups.items():
            sub = _folder(f_ov, overlay_id)
            for pf in items:
                g = pf.geom or pf.geom_simpl
                if not g:
                    continue
                g = _simple_or_same(
                    g, tol_polys if g.geom_type.endswith("Polygon") else tol_lines)
                _put_geom(sub, g, name=pf.properties.get("name") or overlay_id)

    # 3) Camadas PostGIS conforme flags
    #    (troque o recorte para annotate(Intersection) se quiser 100% SQL)
    if layer_flags.get("rios"):
        f = _folder(kml, "Rios")
        qs = Waterway.objects.all().only("geom")
        _query_and_draw(f, qs, aoi, tol_lines, default_name="Rio")

    if layer_flags.get("lt"):
        f = _folder(kml, "Linhas de Transmissão")
        qs = LinhaTransmissao.objects.all().only("geom", "name")
        _query_and_draw(f, qs, aoi, tol_lines,
                        name_field="name", default_name="LT")

    if layer_flags.get("mf"):
        f = _folder(kml, "Malha Ferroviária")
        qs = MalhaFerroviaria.objects.all().only("geom", "name")
        _query_and_draw(f, qs, aoi, tol_lines,
                        name_field="name", default_name="Ferrovia")

    if layer_flags.get("cidades"):
        f = _folder(kml, "Municípios")
        qs = Cidade.objects.all().only("geom", "name")
        _query_and_draw(f, qs, aoi, tol_polys,
                        name_field="name", default_name="Município")

    if layer_flags.get("limites_federais"):
        f = _folder(kml, "Limites Federais")
        qs = LimiteFederal.objects.all().only("geom", "name")
        _query_and_draw(f, qs, aoi, tol_polys, name_field="name",
                        default_name="Limite Federal")

    if layer_flags.get("areas_estaduais"):
        f = _folder(kml, "Áreas Estaduais")
        qs = Area.objects.filter(
            source__icontains="estad").only("geom", "name")
        _query_and_draw(f, qs, aoi, tol_polys, name_field="name",
                        default_name="Área Estadual")

    # 4) KML ou KMZ
    base = slugify(f"projeto-{project.id}")
    if out_format.lower() == "kml":
        payload = kml.kml().encode("utf-8")
        return payload, f"{base}.kml", "application/vnd.google-earth.kml+xml"
    else:
        kml_bytes = kml.kml().encode("utf-8")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("doc.kml", kml_bytes)
        return buf.getvalue(), f"{base}.kmz", "application/vnd.google-earth.kmz"
        return buf.getvalue(), f"{base}.kmz", "application/vnd.google-earth.kmz"
