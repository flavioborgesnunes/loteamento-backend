from __future__ import annotations

import io
import json
import zipfile
from typing import Dict, Iterable, List, Optional, Tuple

from django.contrib.gis.db.models import GeometryField
from django.contrib.gis.db.models.functions import Intersection, MakeValid
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon
from django.db import connection
from django.db.models import F, Func, Value
from django.utils.text import slugify
# MODELS PostGIS
from geodata.models import (Area, Cidade, LimiteFederal, LinhaTransmissao,
                            MalhaFerroviaria)
from rios.models import Waterway

try:
    import simplekml
except Exception:
    simplekml = None


# ============================ Helpers DB / GEOS ============================

def _refresh_conn():
    try:
        connection.close_if_unusable_or_obsolete()
    except Exception:
        try:
            connection.close()
        except Exception:
            pass


def _yield_ids_in_batches(qs_ids, batch_size=2000):
    batch = []
    for _id in qs_ids:
        batch.append(_id)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _to_geos(obj) -> GEOSGeometry:
    g = GEOSGeometry(json.dumps(obj) if isinstance(obj, dict) else obj)
    if g.srid is None:
        g.srid = 4326
    if g.srid != 4326:
        g.transform(4326)
    if not g.valid:
        try:
            g = g.buffer(0)
        except Exception:
            pass
    return g


def _ensure_mp(g: GEOSGeometry) -> MultiPolygon:
    if g.geom_type == "Polygon":
        return MultiPolygon([g], srid=4326)
    if g.geom_type != "MultiPolygon":
        raise ValueError("AOI deve ser Polygon/MultiPolygon.")
    return g


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


# ============================ KML helpers (XYZ e M) ============================

def _split_xyz_m(pt: Iterable[float]) -> Tuple[Tuple[float, ...], Optional[float]]:
    x = float(pt[0])
    y = float(pt[1])
    if len(pt) >= 3:
        z = float(pt[2])
        xyz = (x, y, z)
        m = float(pt[3]) if len(pt) >= 4 else None
        return xyz, m
    return (x, y), None


def _coords_for_kml_line(geom) -> Tuple[List[Tuple[float, ...]], List[float]]:
    coords_xyz, m_vals = [], []
    for pt in geom.coords:
        xyz, m = _split_xyz_m(pt)
        coords_xyz.append(xyz)
        if m is not None:
            m_vals.append(m)
    return coords_xyz, m_vals


def _extract_lines(geos_geom):
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


def _color_a(alpha_int, rgb_name):
    from simplekml import Color
    base = getattr(Color, rgb_name, Color.white)
    return Color.changealphaint(alpha_int, base)


def _add_lines_to_kml(folder, geos_geom, line_color, name_prefix):
    for ln in _extract_lines(geos_geom):
        coords_xyz, m_vals = _coords_for_kml_line(ln)
        if not coords_xyz:
            continue
        ls = folder.newlinestring(name=name_prefix, coords=coords_xyz)
        ls.style.linestyle.width = 2
        ls.style.linestyle.color = line_color
        if m_vals:
            try:
                ls.extendeddata.newdata(
                    name="m_values", value=json.dumps(m_vals))
            except Exception:
                pass


def _add_polygons_to_kml(folder, gj_geom, line_color, name_prefix, fill_alpha: int = 0):
    """Polígonos com **preenchimento opcional** (por padrão 0 → só contorno)."""
    def clean_ring(ring):
        out = []
        for pt in ring:
            xyz, _m = _split_xyz_m(pt)
            out.append(xyz)
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
        # **AOI sem preencher**: fill=0; outras camadas podem usar alpha baixo se quiser
        p.style.polystyle.color = _color_a(fill_alpha, "white")
        p.style.polystyle.fill = 1 if fill_alpha > 0 else 0
        p.style.linestyle.color = line_color
        p.style.linestyle.width = 2

    t = (gj_geom.get("type") or "").lower()
    if t == "polygon":
        add_polygon(gj_geom["coordinates"], name_prefix)
    elif t == "multipolygon":
        for i, coords in enumerate(gj_geom["coordinates"], 1):
            add_polygon(coords, f"{name_prefix} {i}")


def _overlay_palette(idx):
    names = ["red", "orange", "yellow", "green",
             "cyan", "blue", "purple", "white"]
    name = names[idx % len(names)]
    line = getattr(simplekml.Color, name, simplekml.Color.white)
    return line


# ============================ Builder principal ============================

def build_kmz_from_payload(
    *,
    project,                 # instancia Project
    aoi_geojson: dict,
    layer_flags: Dict,
    simplify: Dict | None = None,
    include_saved_overlays: bool = True,
    out_format: str = "kmz",
) -> Tuple[bytes, str, str]:
    """
    Gera KML/KMZ com pastas:
      - AOI (somente CONTORNO)  <-- não tampa as camadas
      - Rios, LT, Ferrovias, Municípios, Áreas Federais, Áreas Estaduais (DB)
      - Overlays Secundários (ProjectFeature) em subpastas por overlay_id
    Usa recorte e simplificação no banco (ST_Intersection + ST_SimplifyPreserveTopology).
    """
    if simplekml is None:
        raise RuntimeError("simplekml não instalado. pip install simplekml")

    simplify = simplify or {}
    tol_lines = float(simplify.get("lines", simplify.get(
        "rios", simplify.get("lt", 0))) or 0) or 0.00002
    tol_polys = float(simplify.get(
        "polygons", simplify.get("polygon", 0)) or 0) or 0.00005

    aoi = _ensure_mp(_to_geos(aoi_geojson))

    kml = simplekml.Kml()

    # 0) AOI (apenas contorno)
    fld_aoi = kml.newfolder(name="AOI")
    try:
        aoi_gj = json.loads(aoi.json)
        _add_polygons_to_kml(
            folder=fld_aoi,
            gj_geom=aoi_gj,
            line_color=simplekml.Color.cyan,
            name_prefix="AOI",
            fill_alpha=0,           # <<<<<<<<<<<<<<  sem preenchimento
        )
    except Exception:
        pass

    total = 0

    # ---------- 1) Rios ----------
    if layer_flags.get("rios"):
        fld_rios = None
        ids_qs = (
            Waterway.objects
            .filter(geom__intersects=aoi)
            .order_by("id")
            .values_list("id", flat=True)
        )
        for id_batch in _yield_ids_in_batches(ids_qs, batch_size=2000):
            try:
                qs = (
                    Waterway.objects
                    .filter(id__in=id_batch)
                    .annotate(clipped=Intersection("geom", Value(aoi, output_field=GeometryField(srid=4326))))
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_lines).only("id")
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
            except Exception:
                _refresh_conn()
                continue

    # ---------- 2) Linhas de Transmissão ----------
    if layer_flags.get("lt"):
        fld_lt = None
        ids_qs = (
            LinhaTransmissao.objects
            .filter(geom__intersects=aoi)
            .order_by("id")
            .values_list("id", flat=True)
        )
        for id_batch in _yield_ids_in_batches(ids_qs, batch_size=2000):
            try:
                qs = (
                    LinhaTransmissao.objects
                    .filter(id__in=id_batch)
                    .annotate(clipped=Intersection("geom", Value(aoi, output_field=GeometryField(srid=4326))))
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_lines).only("id")
                for row in qs:
                    for ln in _extract_lines(row.geom_simpl):
                        coords_xyz, m_vals = _coords_for_kml_line(ln)
                        if not coords_xyz:
                            continue
                        if fld_lt is None:
                            fld_lt = kml.newfolder(
                                name="Linhas de Transmissão")
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
            except Exception:
                _refresh_conn()
                continue

    # ---------- 3) Malha Ferroviária ----------
    if layer_flags.get("mf"):
        fld_mf = None
        ids_qs = (
            MalhaFerroviaria.objects
            .filter(geom__intersects=aoi)
            .order_by("id")
            .values_list("id", flat=True)
        )
        for id_batch in _yield_ids_in_batches(ids_qs, batch_size=2000):
            try:
                qs = (
                    MalhaFerroviaria.objects
                    .filter(id__in=id_batch)
                    .annotate(clipped=Intersection("geom", Value(aoi, output_field=GeometryField(srid=4326))))
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_lines).only("id")
                for row in qs:
                    for ln in _extract_lines(row.geom_simpl):
                        coords_xyz, m_vals = _coords_for_kml_line(ln)
                        if not coords_xyz:
                            continue
                        if fld_mf is None:
                            fld_mf = kml.newfolder(name="Ferrovias")
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
            except Exception:
                _refresh_conn()
                continue

    # ---------- 4) Cidades ----------
    if layer_flags.get("cidades"):
        fld_cidades = None
        ids_qs = (
            Cidade.objects
            .filter(geom__intersects=aoi)
            .order_by("id")
            .values_list("id", flat=True)
        )
        for id_batch in _yield_ids_in_batches(ids_qs, batch_size=1000):
            try:
                qs = (
                    Cidade.objects
                    .filter(id__in=id_batch)
                    .annotate(clipped=Intersection("geom", Value(aoi, output_field=GeometryField(srid=4326))))
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_polys).only("id")
                for row in qs:
                    gj = json.loads(row.geom_simpl.json)
                    if fld_cidades is None:
                        fld_cidades = kml.newfolder(name="Municípios")
                    _add_polygons_to_kml(
                        folder=fld_cidades,
                        gj_geom=gj,
                        line_color=simplekml.Color.yellow,
                        name_prefix="Município",
                        fill_alpha=40,
                    )
                    total += 1
            except Exception:
                _refresh_conn()
                continue

    # ---------- 5) Áreas Federais ----------
    if layer_flags.get("limites_federais"):
        fld_fed = None
        ids_qs = (
            LimiteFederal.objects
            .filter(geom__intersects=aoi)
            .order_by("id")
            .values_list("id", flat=True)
        )
        for id_batch in _yield_ids_in_batches(ids_qs, batch_size=1000):
            try:
                qs = (
                    LimiteFederal.objects
                    .filter(id__in=id_batch)
                    .annotate(clipped=Intersection("geom", Value(aoi, output_field=GeometryField(srid=4326))))
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_polys).only("id")
                for row in qs:
                    gj = json.loads(row.geom_simpl.json)
                    if fld_fed is None:
                        fld_fed = kml.newfolder(name="Áreas Federais")
                    _add_polygons_to_kml(
                        folder=fld_fed,
                        gj_geom=gj,
                        line_color=simplekml.Color.green,
                        name_prefix="Área Federal",
                        fill_alpha=50,
                    )
                    total += 1
            except Exception:
                _refresh_conn()
                continue

    # ---------- 6) Áreas Estaduais ----------
    if layer_flags.get("areas_estaduais"):
        fld_est = None
        base_qs = Area.objects.filter(geom__intersects=aoi)
        # opcional: filtra por UF do projeto, se existir
        try:
            if getattr(project, "uf", None):
                base_qs = base_qs.filter(uf=project.uf)
        except Exception:
            pass
        ids_qs = base_qs.order_by("id").values_list("id", flat=True)
        for id_batch in _yield_ids_in_batches(ids_qs, batch_size=1000):
            try:
                qs = (
                    Area.objects
                    .filter(id__in=id_batch)
                    .annotate(clipped=Intersection("geom", Value(aoi, output_field=GeometryField(srid=4326))))
                )
                qs = _annotate_clip_simplify(
                    qs, F("clipped"), tol_polys).only("id")
                for row in qs:
                    gj = json.loads(row.geom_simpl.json)
                    if fld_est is None:
                        fld_est = kml.newfolder(name="Áreas Estaduais")
                    _add_polygons_to_kml(
                        folder=fld_est,
                        gj_geom=gj,
                        line_color=simplekml.Color.purple,
                        name_prefix="Área Estadual",
                        fill_alpha=50,
                    )
                    total += 1
            except Exception:
                _refresh_conn()
                continue

    # ---------- 7) Overlays Secundários (salvos no PostGIS) ----------
    if include_saved_overlays:
        from .models import ProjectFeature  # import local para evitar ciclos
        feats = (
            ProjectFeature.objects
            .filter(project=project)
            .only("overlay_id", "properties", "color", "geom", "geom_simpl")
        )

        groups: Dict[str, List] = {}
        for pf in feats.iterator():
            groups.setdefault(pf.overlay_id or "overlay", []).append(pf)

        if groups:
            fld_over = kml.newfolder(name="Overlays")
            for idx, (overlay_id, items) in enumerate(sorted(groups.items(), key=lambda x: x[0])):
                sub = fld_over.newfolder(name=str(overlay_id))
                line_color = _overlay_palette(idx)
                for pf in items:
                    g = pf.geom_simpl or pf.geom
                    if not g or g.empty:
                        continue
                    nm = (pf.properties or {}).get("name") or overlay_id
                    if g.geom_type in ("LineString", "MultiLineString", "GeometryCollection"):
                        _add_lines_to_kml(sub, g, line_color, nm)
                    elif g.geom_type in ("Polygon", "MultiPolygon"):
                        gj = json.loads(g.json)
                        _add_polygons_to_kml(
                            folder=sub,
                            gj_geom=gj,
                            line_color=line_color,
                            name_prefix=nm,
                            fill_alpha=40,
                        )
                    # (points/others ignorados)

    # ---------- Saída ----------
    base = slugify(f"projeto-{project.id}")
    if out_format.lower() == "kml":
        payload = kml.kml().encode("utf-8")
        return payload, f"{base}.kml", "application/vnd.google-earth.kml+xml"
    else:
        kml_bytes = kml.kml().encode("utf-8")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            # mimetype primeiro sem compressão (KMZ válido)
            zinfo = zipfile.ZipInfo("mimetype")
            zinfo.compress_type = zipfile.ZIP_STORED
            zf.writestr(zinfo, b"application/vnd.google-earth.kmz")
            zf.writestr("doc.kml", kml_bytes,
                        compress_type=zipfile.ZIP_DEFLATED)
        return buf.getvalue(), f"{base}.kmz", "application/vnd.google-earth.kmz"
