# restricoes/views.py

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

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, MultiLineString, Polygon
from django.contrib.gis.geos import GeometryCollection

from .models import (
    Restricoes, AreaVerdeV, CorteAreaVerdeV, RuaV,
    MargemRioV, MargemLTV, MargemFerroviaV, SRID_WGS
)
from .serializers import RestricoesSerializer
from projetos.models import Project

SRID_IN = 4326
SRID_WEBMERC = 3857

SNAP_GRID = 1e-7

# --- helpers para unir / diferenciar / medir ---

def _union_mpolys_4674(polys) -> MultiPolygon | None:
    """
    União robusta de vários polígonos. Ignora None/empty.
    Retorna MultiPolygon 4674 ou None.
    """
    acc = None
    for p in polys or []:
        if not p:
            continue
        try:
            mp = _norm_poly_4674(p)  # garante MP 4674
            if mp.empty:
                continue
            acc = mp if acc is None else acc.union(mp)
        except Exception:
            continue
    if not acc:
        return None
    return _ensure_mpoly_4674(acc)

def _diff_clip(aoi_mp_4674: MultiPolygon, sub_mp_4674: MultiPolygon | None) -> MultiPolygon | None:
    """
    Loteável = AOI \ Subtração. Garante MP 4674 e clip final pela AOI.
    """
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
    """
    Área aproximada em m² via 3857 (boa o suficiente aqui).
    """
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

def _ensure_srid(g: GEOSGeometry, srid: int) -> GEOSGeometry:
    """Se g.srid estiver None, define para srid. Retorna um clone."""
    if not g:
        return g
    gg = g.clone()
    if not getattr(gg, "srid", None):
        gg.srid = srid
    return gg

def _ensure_mpoly_4674(g: GEOSGeometry) -> MultiPolygon:
    """Garante MultiPolygon com SRID 4674."""
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
    # último recurso: reinterpreta via geojson
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
    """Loga bbox e centroid em 4326; tolera srid=None."""
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

SNAP_GRID = 1e-7  # ~1cm em 4326 (aprox), melhora robustez topológica

def _force_2d(g: GEOSGeometry) -> GEOSGeometry:
    gg = g.clone()
    try:
        gg = GEOSGeometry(gg.geojson, srid=gg.srid)  # descarta Z/M re-parsando
    except Exception:
        pass
    return gg

def _make_valid(g: GEOSGeometry) -> GEOSGeometry:
    try:
        return g.make_valid()
    except Exception:
        return g

def _snap_4326_small(g: GEOSGeometry) -> GEOSGeometry:
    """Snappa em 4326 para reduzir self-intersections numéricas."""
    gg = _to_srid(g, SRID_IN)
    try:
        return gg.snap_to_grid(SNAP_GRID)
    except Exception:
        return gg

def _norm_line_4674(g: GEOSGeometry) -> MultiLineString:
    """
    Converte qualquer Line/MultiLine/GC de linhas para MultiLineString em SRID 4674,
    preservando 2D, validando, snappando e *forçando* SRID no retorno.
    """
    # Se vier sem SRID, assume 4326
    if not getattr(g, "srid", None):
        try:
            g = GEOSGeometry(g.geojson, srid=SRID_IN)  # 4326
        except Exception:
            g.srid = SRID_IN

    gg = _force_2d(g)
    gg = _make_valid(gg)

    # make_valid pode “perder” SRID
    if not getattr(gg, "srid", None):
        gg.srid = SRID_IN

    # snap em 4326 e vai para 4674
    gg = _snap_4326_small(gg)     # garante 4326
    gg = _to_srid(gg, SRID_WGS)   # 4674

    # Função auxiliar: sempre devolve MultiLineString 4674
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
                    # decompõe em LineStrings
                    parts.extend([ls for ls in sub])
            if not parts:
                raise ValueError("GeometryCollection sem linhas")
            # Garante que todas as partes estão em 4674
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
    # trava de novo por garantia
    if not getattr(ml, "srid", None):
        ml.srid = SRID_WGS
    return ml


def _norm_poly_4674(g: GEOSGeometry) -> MultiPolygon:
    """Polygon/MultiPolygon/GC -> MultiPolygon 4674, 2D, validado, snappado e com SRID forçado."""
    gg = _force_2d(_ensure_srid(g, SRID_IN))  # assume 4326 se None
    gg = _make_valid(gg)
    if not getattr(gg, "srid", None):
        gg.srid = SRID_IN
    gg = _snap_4326_small(gg)
    gg = _to_srid(gg, SRID_WGS)  # 4674

    if gg.geom_type == "Polygon":
        mp = MultiPolygon(gg)
        mp.srid = SRID_WGS
        return mp
    if gg.geom_type == "MultiPolygon":
        gg.srid = SRID_WGS
        return gg
    if gg.geom_type == "GeometryCollection":
        polys = [geom for geom in gg if geom.geom_type in ("Polygon","MultiPolygon")]
        if not polys:
            raise ValueError("Sem Polygon em GeometryCollection")
        acc = None
        for p in polys:
            p2 = _norm_poly_4674(p)  # já retorna MP 4674
            acc = p2 if acc is None else acc.union(p2)
        # union às vezes perde SRID → força
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
    """
    Buffer em metros (3857) de uma linha (espera 4674) + clip pela AOI (4674),
    devolvendo SEMPRE MultiPolygon com SRID 4674 (ou None se vazio).
    """
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
        gb = _norm_poly_4674(gb)  # já devolve MP 4674
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

def _centroid_log(g: GEOSGeometry, label: str):
    try:
        c = _to_srid(g, SRID_IN).centroid  # em 4326 para log
        print(f"[restricoes] {label}: srid={g.srid} centroid(lon,lat)=({c.x:.6f},{c.y:.6f}) empty={g.empty}")
    except Exception:
        print(f"[restricoes] {label}: <centroid fail> srid={getattr(g,'srid',None)}")


def _iter_fc(fc):
    if not fc or fc.get("type") != "FeatureCollection":
        return []
    return fc.get("features") or []

def _from_geojson(geom_dict: Dict[str, Any], srid_default: int = SRID_IN) -> GEOSGeometry:
    """
    Cria GEOSGeometry a partir de dict GeoJSON e GARANTE srid=4326.
    (Alguns ambientes retornam g.srid None ao parsear GeoJSON; aqui forçamos.)
    """
    g = GEOSGeometry(json.dumps(geom_dict))
    # Força 4326 sempre (independente do que vier)
    try:
        g.srid = srid_default  # 4326
    except Exception:
        # fallback extremo: reparse com WKT (raro)
        g = GEOSGeometry(g.wkt, srid=srid_default)
    return g


def _to_srid(g: GEOSGeometry, srid: int) -> GEOSGeometry:
    """Clona e transforma para o SRID desejado."""
    gg = g.clone()
    if gg.srid != srid:
        gg.transform(srid)
    return gg

def _as_mpoly(g: GEOSGeometry) -> MultiPolygon:
    if g.geom_type == "Polygon":
        return MultiPolygon(g)
    if g.geom_type == "MultiPolygon":
        return g
    raise ValueError(f"Esperado Polygon/MultiPolygon, recebi {g.geom_type}")

def _as_mline(g: GEOSGeometry) -> MultiLineString:
    if g.geom_type == "LineString":
        return MultiLineString(g)
    if g.geom_type == "MultiLineString":
        return g
    raise ValueError(f"Esperado LineString/MultiLineString, recebi {g.geom_type}")

def _buffer_meters(g_any_srid: GEOSGeometry, meters: float, target_srid: int = SRID_WGS) -> MultiPolygon:
    """
    Buffer em metros com pipeline estável:
      qualquer SRID -> 4326 -> 3857 -> buffer(m) -> 4326 -> target_srid
    Retorna MultiPolygon em target_srid.
    """
    if not g_any_srid or meters is None or float(meters) <= 0:
        raise ValueError("buffer_meters: geometria/metros inválidos")

    g4326 = _to_srid(g_any_srid, SRID_IN)
    g3857 = _to_srid(g4326, SRID_WEBMERC)
    gb = g3857.buffer(float(meters))

    # volta
    if gb.srid != SRID_WEBMERC:
        gb.transform(SRID_WEBMERC)
    gb.transform(SRID_IN)
    gb.transform(target_srid)

    # garante MultiPolygon
    if gb.geom_type == "Polygon":
        return MultiPolygon(gb)
    if gb.geom_type == "MultiPolygon":
        return gb

    # fallback via geojson
    gj = GEOSGeometry(gb.geojson, srid=target_srid)
    if gj.geom_type == "Polygon":
        return MultiPolygon(gj)
    if gj.geom_type == "MultiPolygon":
        return gj
    raise ValueError(f"Buffer resultou tipo inesperado: {gb.geom_type}")

def _get_prop(props: Dict[str, Any], key: str, default: Any = None) -> Any:
    if not isinstance(props, dict):
        return default
    return props.get(key, default)


# ---------- CREATE versão (salva tudo: eixos + máscaras/faixas) ----------

class RestricoesCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, project_id: int, *args, **kwargs):
        proj = get_object_or_404(Project, pk=project_id)

        # ---- campos básicos
        label = request.data.get("label", "") or ""
        notes = request.data.get("notes", "") or ""
        percent_permitido = request.data.get("percent_permitido", None)
        corte_pct_cache = request.data.get("corte_pct_cache", None)
        source = request.data.get("source", "geoman")

        # ---- ad hoc
        ad_hoc = request.data.get("adHoc") or {}
        aoi_in = ad_hoc.get("aoi")
        if not aoi_in:
            return Response({"detail": "Campo obrigatório ausente: adHoc.aoi"},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            aoi_g = _from_geojson(aoi_in)           # força 4326
            aoi_snapshot = _ensure_mpoly_4674(aoi_g)  # MP 4674
            _debug_geom("AOI", aoi_snapshot)
        except Exception as e:
            return Response({"detail": f"AOI inválida: {e}"}, status=status.HTTP_400_BAD_REQUEST)

        # Collections
        av_fc   = ad_hoc.get("av") or {}
        corte_fc= ad_hoc.get("corte_av") or {}
        ruas_fc = ad_hoc.get("ruas") or {}
        rios_fc = ad_hoc.get("rios") or {}
        lt_fc   = ad_hoc.get("lt") or {}
        fer_fc  = ad_hoc.get("ferrovias") or {}

        # Defaults
        default_rua_width = ad_hoc.get("default_rua_width", 12)
        def_margem_rio = ad_hoc.get("def_margem_rio", 30)
        def_margem_lt = ad_hoc.get("def_margem_lt", 15)
        def_margem_fer = ad_hoc.get("def_margem_fer", 20)

        with transaction.atomic():
            # Cria versão
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

            # ----------------- Áreas Verdes (MultiPolygon 4674) -----------------
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

            # ----------------- Cortes de AV (MultiPolygon 4674) -----------------
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

            # ----------------- Ruas: eixo + mask (clip na AOI) -----------------
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
                    eixo = _norm_line_4674(eixo_raw)         # MLS 4674
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

            # ----------------- Rios: centerline + faixa -----------------
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

            # ----------------- Linhas de Transmissão: centerline + faixa -----------------
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

            # ----------------- Ferrovias: centerline + faixa -----------------
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

            # ----------------- ÁREA LOTEÁVEL -----------------
            # Regra: Loteável = AOI − ( máscaras ∪ faixas ∪ (AV_total − Corte_total) )
            try:
                # 1) máscaras/faixas (infra)
                masks_polys = []
                masks_polys.extend([row.mask for row in rua_bulk if getattr(row, "mask", None)])
                masks_polys.extend([row.faixa for row in rio_bulk if getattr(row, "faixa", None)])
                masks_polys.extend([row.faixa for row in lt_bulk  if getattr(row, "faixa", None)])
                masks_polys.extend([row.faixa for row in fer_bulk if getattr(row, "faixa", None)])
                union_masks = _union_mpolys_4674(masks_polys)

                # 2) AV efetiva
                av_polys    = [row.geom for row in av_bulk]      # AV (MP 4674)
                corte_polys = [row.geom for row in corte_bulk]   # Cortes
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

                # 3) Subtração total
                excl = _union_mpolys_4674([x for x in [union_masks, av_efetiva] if x])

                # 4) Loteável
                loteavel = _diff_clip(aoi_snapshot, excl)

                # 5) Persistir
                r.area_loteavel = loteavel if loteavel and not loteavel.empty else None
                r.save(update_fields=["area_loteavel"])

                if r.area_loteavel:
                    print(f"[restricoes] loteavel area_m2={_area_m2(r.area_loteavel):.2f}")
            except Exception as e:
                print(f"[restricoes] erro ao gerar area_loteavel: {e}")

            # ----------------- resumo p/ resposta -----------------
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
# ---------- LIST versões por projeto ----------

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


# ---------- DETAIL GeoJSON (inclui eixos e faixas/máscaras) ----------

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
import json

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

        # AV / Cortes
        av_feats = [feat(row.geom) for row in r.areas_verdes.all()]
        corte_feats = [feat(row.geom) for row in r.cortes_av.all()]

        # Ruas
        rua_eixo_feats, rua_mask_feats = [], []
        for row in r.ruas.all():
            try:
                if row.eixo:
                    rua_eixo_feats.append({"type":"Feature","geometry": json.loads(row.eixo.geojson),"properties":{"width_m": float(row.largura_m)}})
                if getattr(row, "mask", None):
                    rua_mask_feats.append(feat(row.mask, {"width_m": float(row.largura_m)}))
            except Exception:
                pass

        # Rios
        rios_centerline_feats = []
        rios_faixa_feats = []
        for row in r.margens_rio.all():
            try:
                if row.centerline:
                    rios_centerline_feats.append({"type":"Feature","geometry": json.loads(row.centerline.geojson),"properties":{"margem_m": float(row.margem_m)}})
                if row.faixa:
                    rios_faixa_feats.append(feat(row.faixa, {"margem_m": float(row.margem_m)}))
            except Exception:
                pass

        # LT
        lt_centerline_feats, lt_faixa_feats = [], []
        for row in r.margens_lt.all():
            try:
                if row.centerline:
                    lt_centerline_feats.append({"type":"Feature","geometry": json.loads(row.centerline.geojson),"properties":{"margem_m": float(row.margem_m)}})
                if row.faixa:
                    lt_faixa_feats.append(feat(row.faixa, {"margem_m": float(row.margem_m)}))
            except Exception:
                pass

        # Ferrovias
        fer_centerline_feats, fer_faixa_feats = [], []
        for row in r.margens_ferrovia.all():
            try:
                if row.centerline:
                    fer_centerline_feats.append({"type":"Feature","geometry": json.loads(row.centerline.geojson),"properties":{"margem_m": float(row.margem_m)}})
                if row.faixa:
                    fer_faixa_feats.append(feat(row.faixa, {"margem_m": float(row.margem_m)}))
            except Exception:
                pass

        # Área Loteável
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

            # NEW
            "area_loteavel": loteavel_fc,
        }
        return Response(data, status=status.HTTP_200_OK)