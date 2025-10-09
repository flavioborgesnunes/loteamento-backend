from __future__ import annotations
import json
from typing import Any, Dict, List

from django.db import transaction, models as djmodels
from django.shortcuts import get_object_or_404
from rest_framework import status, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated

from shapely.geometry import shape, mapping
from shapely.ops import unary_union, snap
from shapely.validation import make_valid as shapely_make_valid
from pyproj import Transformer
import math

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, MultiLineString, Polygon
from django.contrib.gis.geos import GeometryCollection

from .models import (
    Restricoes, AreaVerdeV, CorteAreaVerdeV, RuaV,
    MargemRioV, MargemLTV, MargemFerroviaV, SRID_WGS,
    ManualRestricaoV,  # ---- MANUAIS
)
from .serializers import RestricoesSerializer
from projetos.models import Project

SRID_IN = 4326
SRID_WEBMERC = 3857
SNAP_GRID = 1e-7

# --- helpers para unir / diferenciar / medir ---

def _union_mpolys_4674(polys) -> MultiPolygon | None:
    acc = None
    for p in polys or []:
        if not p:
            continue
        try:
            mp = _norm_poly_4674(p)
            if mp.empty:
                continue
            acc = mp if acc is None else acc.union(mp)
        except Exception:
            continue
    if not acc:
        return None
    return _ensure_mpoly_4674(acc)

def _diff_clip(aoi_mp_4674: MultiPolygon, sub_mp_4674: MultiPolygon | None) -> MultiPolygon | None:
    if not aoi_mp_4674 or aoi_mp_4674.empty:
        return None
    if not sub_mp_4674 or sub_mp_4674.empty:
        return _ensure_mpoly_4674(aoi_mp_4674)
    try:
        d = aoi_mp_4674.difference(sub_mp_4674)
        if not d or d.empty:
            return None
        d = _norm_poly_4674(d)
        d = _norm_poly_4674(d.intersection(aoi_mp_4674))
        return None if d.empty else _ensure_mpoly_4674(d)
    except Exception:
        return None

def _area_m2(mp_4674: GEOSGeometry) -> float:
    if not mp_4674 or mp_4674.empty:
        return 0.0
    g = mp_4674.clone()
    try:
        if g.srid != 3857:
            g.transform(3857)
        return float(g.area)
    except Exception:
        return 0.0

# ---------- Helpers mínimos e robustos ----------

def _geos_to_shp(g):
    if not g or g.empty:
        return None
    try:
        return shape(json.loads(g.geojson))
    except Exception:
        return None

def _shp_to_geos(s, srid=SRID_WGS):
    if s is None:
        return None
    try:
        gj = mapping(s)
        g = GEOSGeometry(json.dumps(gj))
        g.srid = srid
        return g
    except Exception:
        return None

def _ensure_srid(g: GEOSGeometry, srid: int) -> GEOSGeometry:
    if not g:
        return g
    gg = g.clone()
    if not getattr(gg, "srid", None):
        gg.srid = srid
    return gg

def _ensure_mpoly_4674(g: GEOSGeometry) -> MultiPolygon:
    if not g:
        return g
    gg = _ensure_srid(g, SRID_WGS)
    if gg.srid != SRID_WGS:
        gg = gg.clone()
        gg.transform(SRID_WGS)
    if gg.geom_type == "Polygon":
        mp = MultiPolygon(gg)
        mp.srid = SRID_WGS
        return mp
    if gg.geom_type == "MultiPolygon":
        gg.srid = SRID_WGS
        return gg
    gj = GEOSGeometry(gg.geojson)
    gj.srid = SRID_WGS
    if gj.geom_type == "Polygon":
        mp = MultiPolygon(gj)
        mp.srid = SRID_WGS
        return mp
    if gj.geom_type == "MultiPolygon":
        gj.srid = SRID_WGS
        return gj
    raise ValueError(f"_ensure_mpoly_4674: tipo inesperado {gg.geom_type}")

def _debug_geom(label: str, g: GEOSGeometry):
    try:
        if not g or g.empty:
            print(f"[restricoes][{label}] EMPTY")
            return
        gg = g.clone()
        if not getattr(gg, "srid", None):
            gg.srid = 4326
        if gg.srid != 4326:
            gg.transform(4326)
        c = gg.centroid
        print(f"[restricoes][{label}] srid={getattr(g,'srid',None)} type={g.geom_type} "
              f"bbox={gg.extent} centroid=({c.x:.6f}, {c.y:.6f})")
    except Exception as e:
        print(f"[restricoes][{label}] ERROR: {e}")

def _force_2d(g: GEOSGeometry) -> GEOSGeometry:
    gg = g.clone()
    try:
        gg = GEOSGeometry(gg.geojson, srid=gg.srid)
    except Exception:
        pass
    return gg

def _make_valid(g: GEOSGeometry) -> GEOSGeometry:
    try:
        return g.make_valid()
    except Exception:
        return g

def _snap_4326_small(g: GEOSGeometry) -> GEOSGeometry:
    gg = _to_srid(g, SRID_IN)
    try:
        return gg.snap_to_grid(SNAP_GRID)
    except Exception:
        return gg

def _norm_line_4674(g: GEOSGeometry) -> MultiLineString:
    if not getattr(g, "srid", None):
        try:
            g = GEOSGeometry(g.geojson, srid=SRID_IN)
        except Exception:
            g.srid = SRID_IN
    gg = _force_2d(g)
    gg = _make_valid(gg)
    if not getattr(gg, "srid", None):
        gg.srid = SRID_IN
    gg = _snap_4326_small(gg)
    gg = _to_srid(gg, SRID_WGS)

    def _as_mls_4674(x: GEOSGeometry) -> MultiLineString:
        if x.geom_type == "LineString":
            ml = MultiLineString(x)
            ml.srid = SRID_WGS
            return ml
        if x.geom_type == "MultiLineString":
            x.srid = SRID_WGS
            return x
        if x.geom_type == "GeometryCollection":
            parts = []
            for sub in x:
                if sub.geom_type == "LineString":
                    parts.append(sub)
                elif sub.geom_type == "MultiLineString":
                    parts.extend([ls for ls in sub])
            if not parts:
                raise ValueError("GeometryCollection sem linhas")
            fixed = []
            for ls in parts:
                if ls.srid != SRID_WGS:
                    ls = ls.clone()
                    if not getattr(ls, "srid", None):
                        ls.srid = SRID_WGS
                    else:
                        ls.transform(SRID_WGS)
                fixed.append(ls)
            ml = MultiLineString(*fixed)
            ml.srid = SRID_WGS
            return ml
        raise ValueError(f"Esperado LineString/MultiLineString, recebi {x.geom_type}")

    ml = _as_mls_4674(gg)
    if not getattr(ml, "srid", None):
        ml.srid = SRID_WGS
    return ml

def _norm_poly_4674(g: GEOSGeometry) -> MultiPolygon:
    gg = _force_2d(_ensure_srid(g, SRID_IN))
    gg = _make_valid(gg)
    if not getattr(gg, "srid", None):
        gg.srid = SRID_IN
    gg = _snap_4326_small(gg)
    gg = _to_srid(gg, SRID_WGS)
    if gg.geom_type == "Polygon":
        mp = MultiPolygon(gg)
        mp.srid = SRID_WGS
        return mp
    if gg.geom_type == "MultiPolygon":
        gg.srid = SRID_WGS
        return gg
    if gg.geom_type == "GeometryCollection":
        polys = [geom for geom in gg if geom.geom_type in ("Polygon", "MultiPolygon")]
        if not polys:
            raise ValueError("Sem Polygon em GeometryCollection")
        acc = None
        for p in polys:
            p2 = _norm_poly_4674(p)
            acc = p2 if acc is None else acc.union(p2)
        acc = _ensure_srid(acc, SRID_WGS)
        if acc.geom_type == "Polygon":
            mp = MultiPolygon(acc)
            mp.srid = SRID_WGS
            return mp
        if acc.geom_type == "MultiPolygon":
            acc.srid = SRID_WGS
            return acc
    raise ValueError(f"Esperado Polygon/MultiPolygon, recebi {gg.geom_type}")

def _buffer_meters_stable_clip_aoi(line_4674: GEOSGeometry, meters: float, aoi_4674: MultiPolygon) -> MultiPolygon | None:
    if not line_4674 or meters is None or float(meters) <= 0:
        return None
    line_4674 = _ensure_srid(line_4674, SRID_WGS)
    aoi_4674  = _ensure_mpoly_4674(aoi_4674)
    try:
        g4326 = _to_srid(line_4674, SRID_IN)
        g3857 = _to_srid(g4326, SRID_WEBMERC)
        gb = g3857.buffer(float(meters))
        if gb.srid != SRID_WEBMERC:
            gb.transform(SRID_WEBMERC)
        gb.transform(SRID_IN)
        gb.transform(SRID_WGS)
        gb = _ensure_srid(gb, SRID_WGS)
    except Exception:
        return None
    try:
        gb = _norm_poly_4674(gb)
    except Exception:
        return None
    try:
        clipped = gb.intersection(aoi_4674)
        if not clipped or clipped.empty:
            return None
        clipped = _norm_poly_4674(_ensure_srid(clipped, SRID_WGS))
        if clipped.empty:
            return None
        return _ensure_mpoly_4674(clipped)
    except Exception:
        return None

def _iter_fc(fc):
    if not fc or fc.get("type") != "FeatureCollection":
        return []
    return fc.get("features") or []

def _from_geojson(geom_dict: Dict[str, Any], srid_default: int = SRID_IN) -> GEOSGeometry:
    g = GEOSGeometry(json.dumps(geom_dict))
    try:
        g.srid = srid_default
    except Exception:
        g = GEOSGeometry(g.wkt, srid=srid_default)
    return g

def _to_srid(g: GEOSGeometry, srid: int) -> GEOSGeometry:
    gg = g.clone()
    if gg.srid != srid:
        gg.transform(srid)
    return gg

def _get_prop(props: Dict[str, Any], key: str, default: Any = None) -> Any:
    if not isinstance(props, dict):
        return default
    return props.get(key, default)

# ---------- Remover Filete ------------

def _pick_utm_epsg_from_lonlat(lon, lat):
    zone = int(math.floor((lon + 180) / 6) + 1)
    south = lat < 0
    mapping_utm_sul = {18:31978, 19:31979, 20:31980, 21:31981, 22:31982, 23:31983, 24:31984, 25:31985}
    if south and zone in mapping_utm_sul:
        return mapping_utm_sul[zone]
    return 3857

def _to_metric_transformers(geom_4674):
    lon, lat = geom_4674.representative_point().x, geom_4674.representative_point().y
    epsg = _pick_utm_epsg_from_lonlat(lon, lat)
    fwd = Transformer.from_crs(4674, epsg, always_xy=True).transform
    rev = Transformer.from_crs(epsg, 4674, always_xy=True).transform
    return fwd, rev

def _proj(geom, fn):
    try:
        return geom if geom is None else geom.transform(fn)
    except Exception:
        from shapely.ops import transform
        return geom if geom is None else transform(fn, geom)

def _clean_union(polys):
    shp_list = []
    for p in polys or []:
        if not p:
            continue
        if hasattr(p, "geojson"):
            p = _geos_to_shp(p)
        if p is None:
            continue
        if getattr(p, "is_empty", False):
            continue
        shp_list.append(p)
    if not shp_list:
        return None
    u = unary_union(shp_list)
    try:
        u = shapely_make_valid(u)
    except Exception:
        pass
    return (u if (u is not None and not u.is_empty) else None)

def _drop_small_parts(geom, min_area_m2=0.05):
    if not geom or geom.is_empty:
        return None
    if geom.geom_type == "Polygon":
        return geom if geom.area >= min_area_m2 else None
    if geom.geom_type == "MultiPolygon":
        parts = [p for p in geom.geoms if p.area >= min_area_m2]
        if not parts:
            return None
        from shapely.geometry import MultiPolygon as ShpMP
        return ShpMP(parts)
    return geom

def robust_diff_m(aoi_4674, excl_4674, snap_tol_m=0.05, eps_m=0.02, min_area_m2=0.05):
    if not aoi_4674 or aoi_4674.is_empty:
        return None
    if not excl_4674 or excl_4674.is_empty:
        return shapely_make_valid(aoi_4674)

    fwd, rev = _to_metric_transformers(aoi_4674)
    aoi_m  = _proj(shapely_make_valid(aoi_4674), fwd)
    excl_m = _proj(shapely_make_valid(excl_4674), fwd)

    aoi_s  = snap(aoi_m,  excl_m, snap_tol_m)
    excl_s = snap(excl_m, aoi_s,  snap_tol_m)

    excl_grow = excl_s.buffer(eps_m)
    diff_raw  = aoi_s.difference(excl_grow)
    diff_fix  = diff_raw.buffer(-eps_m)

    try:
        diff_fix = shapely_make_valid(diff_fix)
    except Exception:
        pass
    if diff_fix and not diff_fix.is_empty:
        diff_fix = _drop_small_parts(diff_fix, min_area_m2=min_area_m2)
        return _proj(diff_fix, rev)
    return None

# ---------- CREATE versão ----------

class RestricoesCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, project_id: int, *args, **kwargs):
        proj = get_object_or_404(Project, pk=project_id)

        label = request.data.get("label", "") or ""
        notes = request.data.get("notes", "") or ""
        percent_permitido = request.data.get("percent_permitido", None)
        corte_pct_cache = request.data.get("corte_pct_cache", None)
        source = request.data.get("source", "geoman")

        ad_hoc = request.data.get("adHoc") or {}
        aoi_in = ad_hoc.get("aoi")
        if not aoi_in:
            return Response({"detail": "Campo obrigatório ausente: adHoc.aoi"},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            aoi_g = _from_geojson(aoi_in)
            aoi_snapshot = _ensure_mpoly_4674(aoi_g)
            _debug_geom("AOI", aoi_snapshot)
        except Exception as e:
            return Response({"detail": f"AOI inválida: {e}"}, status=status.HTTP_400_BAD_REQUEST)

        av_fc        = ad_hoc.get("av") or {}
        corte_fc     = ad_hoc.get("corte_av") or {}
        ruas_fc      = ad_hoc.get("ruas") or {}
        rios_fc      = ad_hoc.get("rios") or {}
        lt_fc        = ad_hoc.get("lt") or {}
        fer_fc       = ad_hoc.get("ferrovias") or {}
        manuais_fc   = ad_hoc.get("manuais") or {}   # ---- MANUAIS

        default_rua_width = ad_hoc.get("default_rua_width", 12)
        def_margem_rio = ad_hoc.get("def_margem_rio", 30)
        def_margem_lt  = ad_hoc.get("def_margem_lt", 15)
        def_margem_fer = ad_hoc.get("def_margem_fer", 20)

        with transaction.atomic():
            r = Restricoes.objects.create(
                project=proj,
                aoi_snapshot=aoi_snapshot,
                label=label,
                notes=notes,
                percent_permitido=percent_permitido,
                corte_pct_cache=corte_pct_cache,
                source=source,
                created_by=request.user if request.user.is_authenticated else None,
            )

            # AV
            av_bulk = []
            for feat in _iter_fc(av_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                try:
                    g = _ensure_mpoly_4674(_from_geojson(geom))
                except Exception:
                    continue
                av_bulk.append(AreaVerdeV(restricoes=r, geom=g))
            if av_bulk:
                AreaVerdeV.objects.bulk_create(av_bulk, batch_size=500)

            # CORTES
            corte_bulk = []
            for feat in _iter_fc(corte_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                try:
                    g = _ensure_mpoly_4674(_from_geojson(geom))
                except Exception:
                    continue
                corte_bulk.append(CorteAreaVerdeV(restricoes=r, geom=g))
            if corte_bulk:
                CorteAreaVerdeV.objects.bulk_create(corte_bulk, batch_size=500)

            # RUAS
            rua_bulk = []
            for feat in _iter_fc(ruas_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                props = feat.get("properties") or {}
                largura = _get_prop(props, "width_m", default_rua_width)
                try:
                    largura = float(largura) if largura is not None else float(default_rua_width)
                except Exception:
                    largura = float(default_rua_width or 12)
                try:
                    eixo_raw = _from_geojson(geom)
                    eixo = _norm_line_4674(eixo_raw)
                    if not getattr(eixo, "srid", None):
                        eixo.srid = SRID_WGS
                    _debug_geom("rua.eixo", eixo)
                except Exception:
                    continue
                mask = _buffer_meters_stable_clip_aoi(eixo, largura/2.0, aoi_snapshot)
                if mask:
                    mask = _ensure_mpoly_4674(mask)
                _debug_geom("rua.mask", mask)
                try:
                    rua_bulk.append(RuaV(restricoes=r, eixo=eixo, largura_m=largura, mask=mask))
                except TypeError:
                    rua_bulk.append(RuaV(restricoes=r, eixo=eixo, largura_m=largura))
            if rua_bulk:
                RuaV.objects.bulk_create(rua_bulk, batch_size=500)

            # RIOS
            rio_bulk = []
            for feat in _iter_fc(rios_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                props = feat.get("properties") or {}
                margem_val = _get_prop(props, "margem_m", def_margem_rio)
                try:
                    margem = float(margem_val) if margem_val is not None else float(def_margem_rio)
                except Exception:
                    margem = float(def_margem_rio)
                try:
                    line_raw = _from_geojson(geom)
                    line = _norm_line_4674(line_raw)
                    if not getattr(line, "srid", None):
                        line.srid = SRID_WGS
                    _debug_geom("rio.centerline", line)
                except Exception:
                    continue
                faixa = _buffer_meters_stable_clip_aoi(line, margem, aoi_snapshot)
                if faixa:
                    faixa = _ensure_mpoly_4674(faixa)
                _debug_geom("rio.faixa", faixa)
                rio_bulk.append(MargemRioV(restricoes=r, centerline=line, margem_m=margem, faixa=faixa))
            if rio_bulk:
                MargemRioV.objects.bulk_create(rio_bulk, batch_size=500)

            # LT
            lt_bulk = []
            for feat in _iter_fc(lt_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                props = feat.get("properties") or {}
                margem_val = _get_prop(props, "margem_m", def_margem_lt)
                try:
                    margem = float(margem_val) if margem_val is not None else float(def_margem_lt)
                except Exception:
                    margem = float(def_margem_lt)
                try:
                    line_raw = _from_geojson(geom)
                    line = _norm_line_4674(line_raw)
                    if not getattr(line, "srid", None):
                        line.srid = SRID_WGS
                    _debug_geom("lt.centerline", line)
                except Exception:
                    continue
                faixa = _buffer_meters_stable_clip_aoi(line, margem, aoi_snapshot)
                if faixa:
                    faixa = _ensure_mpoly_4674(faixa)
                _debug_geom("lt.faixa", faixa)
                lt_bulk.append(MargemLTV(restricoes=r, centerline=line, margem_m=margem, faixa=faixa))
            if lt_bulk:
                MargemLTV.objects.bulk_create(lt_bulk, batch_size=500)

            # FERROVIAS
            fer_bulk = []
            for feat in _iter_fc(fer_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                props = feat.get("properties") or {}
                margem_val = _get_prop(props, "margem_m", def_margem_fer)
                try:
                    margem = float(margem_val) if margem_val is not None else float(def_margem_fer)
                except Exception:
                    margem = float(def_margem_fer)
                try:
                    line_raw = _from_geojson(geom)
                    line = _norm_line_4674(line_raw)
                    if not getattr(line, "srid", None):
                        line.srid = SRID_WGS
                    _debug_geom("fer.centerline", line)
                except Exception:
                    continue
                faixa = _buffer_meters_stable_clip_aoi(line, margem, aoi_snapshot)
                if faixa:
                    faixa = _ensure_mpoly_4674(faixa)
                _debug_geom("fer.faixa", faixa)
                fer_bulk.append(MargemFerroviaV(restricoes=r, centerline=line, margem_m=margem, faixa=faixa))
            if fer_bulk:
                MargemFerroviaV.objects.bulk_create(fer_bulk, batch_size=500)

            # ---- MANUAIS (polígonos convertidos ou desenhados) ----
            manuais_bulk = []
            for feat in _iter_fc(manuais_fc):
                geom = feat.get("geometry")
                props = feat.get("properties") or {}
                if not geom:
                    continue
                try:
                    g = _ensure_mpoly_4674(_from_geojson(geom))
                    nm = str(props.get("name") or props.get("nome") or "").strip()
                    manuais_bulk.append(ManualRestricaoV(restricoes=r, name=nm, geom=g))
                except Exception:
                    continue
            if manuais_bulk:
                ManualRestricaoV.objects.bulk_create(manuais_bulk, batch_size=500)

            # ---------- ÁREA LOTEÁVEL ----------
            try:
                masks_polys = []
                masks_polys.extend([row.mask for row in rua_bulk if getattr(row, "mask", None)])
                masks_polys.extend([row.faixa for row in rio_bulk if getattr(row, "faixa", None)])
                masks_polys.extend([row.faixa for row in lt_bulk  if getattr(row, "faixa", None)])
                masks_polys.extend([row.faixa for row in fer_bulk if getattr(row, "faixa", None)])
                union_masks = _union_mpolys_4674(masks_polys)

                av_polys    = [row.geom for row in av_bulk]
                corte_polys = [row.geom for row in corte_bulk]
                av_total    = _union_mpolys_4674(av_polys)
                corte_total = _union_mpolys_4674(corte_polys)
                av_efetiva  = None
                if av_total:
                    if corte_total:
                        try:
                            av_efetiva = _norm_poly_4674(av_total.difference(corte_total))
                            if av_efetiva and av_efetiva.empty:
                                av_efetiva = None
                        except Exception:
                            av_efetiva = None
                    else:
                        av_efetiva = av_total

                # MANUAIS entram no excluído
                manuais_polys = [row.geom for row in manuais_bulk]
                union_manuais = _union_mpolys_4674(manuais_polys)

                excl_shp = _clean_union([x for x in [union_masks, av_efetiva, union_manuais] if x])

                aoi_shp = _geos_to_shp(aoi_snapshot)
                loteavel_shp = robust_diff_m(
                    aoi_shp, excl_shp,
                    snap_tol_m=0.05,
                    eps_m=0.02,
                    min_area_m2=0.05
                )

                loteavel = _ensure_mpoly_4674(_shp_to_geos(loteavel_shp, srid=SRID_WGS)) if loteavel_shp else None
                r.area_loteavel = loteavel if loteavel and not loteavel.empty else None
                r.save(update_fields=["area_loteavel"])

                if r.area_loteavel:
                    print(f"[restricoes] loteavel area_m2={_area_m2(r.area_loteavel):.2f}")
            except Exception as e:
                print(f"[restricoes] erro ao gerar area_loteavel: {e}")

            qs = (
                Restricoes.objects
                .filter(pk=r.pk)
                .annotate(
                    areas_verdes_count=djmodels.Count("areas_verdes"),
                    cortes_av_count=djmodels.Count("cortes_av"),
                    margens_rio_count=djmodels.Count("margens_rio"),
                    margens_lt_count=djmodels.Count("margens_lt"),
                    margens_ferrovia_count=djmodels.Count("margens_ferrovia"),
                    ruas_count=djmodels.Count("ruas"),
                )
                .first()
            )

        data = RestricoesSerializer(qs).data
        return Response(data, status=status.HTTP_201_CREATED)

# ---------- LIST ----------

class RestricoesListByProjectAPIView(ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = RestricoesSerializer

    def get_queryset(self):
        project_id = self.kwargs["project_id"]
        return (
            Restricoes.objects
            .filter(project_id=project_id)
            .annotate(
                areas_verdes_count=djmodels.Count("areas_verdes"),
                cortes_av_count=djmodels.Count("cortes_av"),
                margens_rio_count=djmodels.Count("margens_rio"),
                margens_lt_count=djmodels.Count("margens_lt"),
                margens_ferrovia_count=djmodels.Count("margens_ferrovia"),
                ruas_count=djmodels.Count("ruas"),
            )
            .order_by("-created_at")
        )

# ---------- DETAIL ----------

class RestricoesGeoDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, restricoes_id: int, *args, **kwargs):
        r = get_object_or_404(Restricoes, pk=restricoes_id)

        def fc(features):
            return {"type":"FeatureCollection","features":features}
        def feat(g, props=None):
            if not g:
                return None
            try:
                return {"type":"Feature","geometry": json.loads(g.geojson), "properties": props or {}}
            except Exception:
                return None

        print(f"[restricoes.detail] GET restricoes_id={r.id} project={r.project_id} version={r.version}")
        if r.aoi_snapshot:
            _debug_geom("AOI", r.aoi_snapshot)

        av_feats = [feat(row.geom) for row in r.areas_verdes.all()]
        corte_feats = [feat(row.geom) for row in r.cortes_av.all()]

        rua_eixo_feats, rua_mask_feats = [], []
        for row in r.ruas.all():
            try:
                if row.eixo:
                    rua_eixo_feats.append({"type":"Feature","geometry": json.loads(row.eixo.geojson),"properties":{"width_m": float(row.largura_m)}})
                if getattr(row, "mask", None):
                    rua_mask_feats.append(feat(row.mask, {"width_m": float(row.largura_m)}))
            except Exception:
                pass

        rios_centerline_feats, rios_faixa_feats = [], []
        for row in r.margens_rio.all():
            try:
                if row.centerline:
                    rios_centerline_feats.append({"type":"Feature","geometry": json.loads(row.centerline.geojson),"properties":{"margem_m": float(row.margem_m)}})
                if row.faixa:
                    rios_faixa_feats.append(feat(row.faixa, {"margem_m": float(row.margem_m)}))
            except Exception:
                pass

        lt_centerline_feats, lt_faixa_feats = [], []
        for row in r.margens_lt.all():
            try:
                if row.centerline:
                    lt_centerline_feats.append({"type":"Feature","geometry": json.loads(row.centerline.geojson),"properties":{"margem_m": float(row.margem_m)}})
                if row.faixa:
                    lt_faixa_feats.append(feat(row.faixa, {"margem_m": float(row.margem_m)}))
            except Exception:
                pass

        fer_centerline_feats, fer_faixa_feats = [], []
        for row in r.margens_ferrovia.all():
            try:
                if row.centerline:
                    fer_centerline_feats.append({"type":"Feature","geometry": json.loads(row.centerline.geojson),"properties":{"margem_m": float(row.margem_m)}})
                if row.faixa:
                    fer_faixa_feats.append(feat(row.faixa, {"margem_m": float(row.margem_m)}))
            except Exception:
                pass

        # ---- MANUAIS
        manuais_feats = []
        for row in r.restricoes_manuais.all():
            try:
                manuais_feats.append({
                    "type":"Feature",
                    "geometry": json.loads(row.geom.geojson),
                    "properties": {"name": row.name or ""},
                })
            except Exception:
                pass

        loteavel_geom = getattr(r, "area_loteavel", None)
        loteavel_fc = {"type":"FeatureCollection","features":[]}
        if loteavel_geom and not loteavel_geom.empty:
            try:
                loteavel_fc["features"].append({
                    "type":"Feature",
                    "geometry": json.loads(loteavel_geom.geojson),
                    "properties": {"area_m2": round(_area_m2(loteavel_geom), 2)},
                })
            except Exception:
                pass

        data = {
            "restricoes_id": r.id,
            "project_id": r.project_id,
            "version": r.version,
            "label": r.label,
            "notes": r.notes,
            "created_at": r.created_at,
            "srid": SRID_WGS,

            "aoi": json.loads(r.aoi_snapshot.geojson) if r.aoi_snapshot else None,

            "av": fc([x for x in av_feats if x]),
            "corte_av": fc([x for x in corte_feats if x]),

            "ruas_eixo": fc([x for x in rua_eixo_feats if x]),
            "ruas_mask": fc([x for x in rua_mask_feats if x]),

            "rios_centerline": fc([x for x in rios_centerline_feats if x]),
            "rios_faixa": fc([x for x in rios_faixa_feats if x]),

            "lt_centerline": fc([x for x in lt_centerline_feats if x]),
            "lt_faixa": fc([x for x in lt_faixa_feats if x]),

            "ferrovias_centerline": fc([x for x in fer_centerline_feats if x]),
            "ferrovias_faixa": fc([x for x in fer_faixa_feats if x]),

            "manuais": fc([x for x in manuais_feats if x]),  # ---- MANUAIS

            "area_loteavel": loteavel_fc,
        }
        return Response(data, status=status.HTTP_200_OK)
