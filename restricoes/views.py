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
    """Loga bbox e centróide; se SRID vier None, assume 4326 só para log."""
    try:
        if not g or g.empty:
            print(f"[restricoes][{label}] EMPTY")
            return
        g_log = g.clone()
        if not getattr(g_log, "srid", None):
            g_log.srid = 4326  # assume WGS84 para log
        g4326 = g_log.clone()
        if g4326.srid != 4326:
            g4326.transform(4326)
        c = g4326.centroid
        print(f"[restricoes][{label}] srid={g.srid} type={g.geom_type} "
              f"bbox={g4326.extent} centroid=({c.x:.6f}, {c.y:.6f})")
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


def _iter_fc(fc: Dict[str, Any]):
    """Itera com segurança sobre FeatureCollection."""
    if not isinstance(fc, dict) or fc.get("type") != "FeatureCollection":
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

        # Campos simples
        label = request.data.get("label", "") or ""
        notes = request.data.get("notes", "") or ""
        percent_permitido = request.data.get("percent_permitido")
        corte_pct_cache = request.data.get("corte_pct_cache")
        source = request.data.get("source", "geoman")

        # Bloco adHoc (geometrias vindas do front)
        ad_hoc = request.data.get("adHoc") or {}

        # AOI (OBRIGATÓRIA)
        aoi_in = ad_hoc.get("aoi")
        if not aoi_in:
            return Response({"detail": "Campo obrigatório ausente: adHoc.aoi"},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            aoi_g = _from_geojson(aoi_in, SRID_IN)
            aoi_snapshot = _as_mpoly(_to_srid(aoi_g, SRID_WGS))
            _debug_geom("AOI", aoi_snapshot)
        except Exception as e:
            return Response({"detail": f"AOI inválida: {e}"}, status=status.HTTP_400_BAD_REQUEST)

        # Collections
        av_fc       = ad_hoc.get("av") or {}
        corte_fc    = ad_hoc.get("corte_av") or {}
        ruas_fc     = ad_hoc.get("ruas") or {}
        rios_fc     = ad_hoc.get("rios") or {}
        lt_fc       = ad_hoc.get("lt") or {}
        fer_fc      = ad_hoc.get("ferrovias") or {}

        # Defaults
        default_rua_width = float(ad_hoc.get("default_rua_width", 12) or 12)
        def_margem_rio    = float(ad_hoc.get("def_margem_rio", 30) or 30)
        def_margem_lt     = float(ad_hoc.get("def_margem_lt", 15) or 15)
        def_margem_fer    = float(ad_hoc.get("def_margem_fer", 20) or 20)

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

            # --- Áreas Verdes (MultiPolygon 4674)
            av_bulk: List[AreaVerdeV] = []
            for feat in _iter_fc(av_fc):
                geom = feat.get("geometry")
                if not geom: 
                    continue
                try:
                    g = _as_mpoly(_to_srid(_from_geojson(geom), SRID_WGS))
                    if not g.empty:
                        av_bulk.append(AreaVerdeV(restricoes=r, geom=g))
                except Exception:
                    pass
            if av_bulk:
                AreaVerdeV.objects.bulk_create(av_bulk, batch_size=500)

            # --- Cortes de AV (MultiPolygon 4674)
            corte_bulk: List[CorteAreaVerdeV] = []
            for feat in _iter_fc(corte_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                try:
                    g = _as_mpoly(_to_srid(_from_geojson(geom), SRID_WGS))
                    if not g.empty:
                        corte_bulk.append(CorteAreaVerdeV(restricoes=r, geom=g))
                except Exception:
                    pass
            if corte_bulk:
                CorteAreaVerdeV.objects.bulk_create(corte_bulk, batch_size=500)

            # --- Ruas (eixo + mask com clip pela AOI)
            rua_bulk: List[RuaV] = []
            for feat in _iter_fc(ruas_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                props = feat.get("properties") or {}
                largura = _get_prop(props, "width_m", default_rua_width)
                try:
                    largura = float(largura) if largura is not None else default_rua_width
                except Exception:
                    largura = default_rua_width
                    
                try:
                    eixo_raw = _from_geojson(geom)
                    eixo = _norm_line_4674(eixo_raw)
                    
                    if not getattr(eixo, "srid", None):
                        eixo.srid = SRID_WGS
                    _debug_geom("rua.eixo", eixo)
                    
                except Exception:
                    continue

                mask = _buffer_meters_stable_clip_aoi(eixo, largura / 2.0, aoi_snapshot)
                _debug_geom("rua.mask", mask)
                if mask:
                    mask = _ensure_mpoly_4674(mask)
                _debug_geom("rua.mask", mask)

                try:
                    rua_bulk.append(RuaV(restricoes=r, eixo=eixo, largura_m=largura, mask=mask))
                except TypeError:
                    rua_bulk.append(RuaV(restricoes=r, eixo=eixo, largura_m=largura))
            if rua_bulk:
                RuaV.objects.bulk_create(rua_bulk, batch_size=500)
                


            # --- Rios (centerline + faixa)
            rio_bulk: List[MargemRioV] = []
            for feat in _iter_fc(rios_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                props = feat.get("properties") or {}
                margem_val = _get_prop(props, "margem_m", def_margem_rio)
                try:
                    margem = float(margem_val) if margem_val is not None else def_margem_rio
                except Exception:
                    margem = def_margem_rio

                try:
                    line_raw = _from_geojson(geom)
                    line = _norm_line_4674(line_raw)
                    
                    if not getattr(line, "srid", None):
                        line.srid = SRID_WGS
                    
                    _debug_geom("rio.centerline", line)
                    
                    _centroid_log(line, "rio.centerline")
                except Exception:
                    continue

                faixa = _buffer_meters_stable_clip_aoi(line, margem, aoi_snapshot)
                if faixa:
                    faixa = _ensure_mpoly_4674(faixa)
                _debug_geom("rio.faixa", faixa)  # (e lt/faixa, fer/faixa)

                
                if faixa:
                    _centroid_log(faixa, "rio.faixa")

                rio_bulk.append(MargemRioV(restricoes=r, centerline=line, margem_m=margem, faixa=faixa))
            if rio_bulk:
                MargemRioV.objects.bulk_create(rio_bulk, batch_size=500)


            # --- Linhas de Transmissão (centerline + faixa)
            lt_bulk: List[MargemLTV] = []
            for feat in _iter_fc(lt_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                props = feat.get("properties") or {}
                margem_val = _get_prop(props, "margem_m", def_margem_lt)
                try:
                    margem = float(margem_val) if margem_val is not None else def_margem_lt
                except Exception:
                    margem = def_margem_lt

                try:
                    line_raw = _from_geojson(geom)
                    line = _norm_line_4674(line_raw)
                    
                    if not getattr(line, "srid", None):
                        line.srid = SRID_WGS
                        
                    _centroid_log(line, "lt.centerline")
                except Exception:
                    continue

                faixa = _buffer_meters_stable_clip_aoi(line, margem, aoi_snapshot)
                if faixa:
                    faixa = _ensure_mpoly_4674(faixa)
                _debug_geom("rio.faixa", faixa)  # (e lt/faixa, fer/faixa)

                
                if faixa:
                    _centroid_log(faixa, "lt.faixa")

                lt_bulk.append(MargemLTV(restricoes=r, centerline=line, margem_m=margem, faixa=faixa))
            if lt_bulk:
                MargemLTV.objects.bulk_create(lt_bulk, batch_size=500)


            # --- Ferrovias (centerline + faixa)
            fer_bulk: List[MargemFerroviaV] = []
            for feat in _iter_fc(fer_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                props = feat.get("properties") or {}
                margem_val = _get_prop(props, "margem_m", def_margem_fer)
                try:
                    margem = float(margem_val) if margem_val is not None else def_margem_fer
                except Exception:
                    margem = def_margem_fer

                try:
                    line_raw = _from_geojson(geom)
                    line = _norm_line_4674(line_raw)
                    
                    if not getattr(line, "srid", None):
                        line.srid = SRID_WGS
                    
                    _centroid_log(line, "fer.centerline")
                except Exception:
                    continue
                
                line = _norm_line_4674(line_raw)
                _debug_geom("fer.centerline", line)

                faixa = _buffer_meters_stable_clip_aoi(line, margem, aoi_snapshot)
                if faixa:
                    faixa = _ensure_mpoly_4674(faixa)
                _debug_geom("rio.faixa", faixa)  # (e lt/faixa, fer/faixa)


                
                if faixa:
                    _centroid_log(faixa, "fer.faixa")

                fer_bulk.append(MargemFerroviaV(restricoes=r, centerline=line, margem_m=margem, faixa=faixa))
            if fer_bulk:
                MargemFerroviaV.objects.bulk_create(fer_bulk, batch_size=500)


            # ---- Resumo p/ resposta (contadores)
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
    permission_classes = [IsAuthenticated]

    def get(self, request, restricoes_id: int, *args, **kwargs):
        from .models import Restricoes, SRID_WGS

        # --- helpers locais de log ---
        def _debug_geom(label, g):
            """Loga bbox e centroid em 4326; tolera srid=None."""
            try:
                if not g or g.empty:
                    print(f"[restricoes.detail][{label}] EMPTY")
                    return
                gg = g.clone()
                if not getattr(gg, "srid", None):
                    gg.srid = 4326
                if gg.srid != 4326:
                    gg.transform(4326)
                c = gg.centroid
                print(f"[restricoes.detail][{label}] srid={getattr(g,'srid',None)} "
                      f"type={g.geom_type} bbox={gg.extent} "
                      f"centroid=({c.x:.6f}, {c.y:.6f})")
            except Exception as e:
                print(f"[restricoes.detail][{label}] ERROR: {e}")

        def fc(features):
            return {"type": "FeatureCollection", "features": features}

        def feat(g, props=None):
            if not g:
                return None
            try:
                return {
                    "type": "Feature",
                    "geometry": json.loads(g.geojson),
                    "properties": props or {},
                }
            except Exception:
                return None

        # --- carrega versão ---
        r = get_object_or_404(Restricoes, pk=restricoes_id)
        print(f"[restricoes.detail] GET restricoes_id={r.id} project={r.project_id} version={r.version}")

        # --- AOI ---
        aoi_geojson = json.loads(r.aoi_snapshot.geojson) if r.aoi_snapshot else None
        if r.aoi_snapshot:
            print(f"[restricoes.detail] SRID_WGS={SRID_WGS} (esperado para persistência)")
            _debug_geom("AOI", r.aoi_snapshot)

        # --- AV e Cortes ---
        av_feats, corte_feats = [], []

        av_qs = list(r.areas_verdes.all())
        print(f"[restricoes.detail] areas_verdes_count={len(av_qs)}")
        for i, row in enumerate(av_qs):
            if i < 3: _debug_geom(f"AV[{i}]", row.geom)
            f = feat(row.geom)
            if f: av_feats.append(f)

        cortes_qs = list(r.cortes_av.all())
        print(f"[restricoes.detail] cortes_av_count={len(cortes_qs)}")
        for i, row in enumerate(cortes_qs):
            if i < 3: _debug_geom(f"CORTE_AV[{i}]", row.geom)
            f = feat(row.geom)
            if f: corte_feats.append(f)

        # --- Ruas (eixos + máscaras) ---
        rua_eixo_feats, rua_mask_feats = [], []
        ruas_qs = list(r.ruas.all())
        print(f"[restricoes.detail] ruas_count={len(ruas_qs)}")
        for i, row in enumerate(ruas_qs):
            try:
                if row.eixo:
                    if i < 3: _debug_geom(f"RUAS.eixo[{i}]", row.eixo)
                    rua_eixo_feats.append({
                        "type": "Feature",
                        "geometry": json.loads(row.eixo.geojson),
                        "properties": {"width_m": float(row.largura_m)},
                    })
                if getattr(row, "mask", None):
                    if i < 3: _debug_geom(f"RUAS.mask[{i}]", row.mask)
                    f = feat(row.mask, {"width_m": float(row.largura_m)})
                    if f: rua_mask_feats.append(f)
            except Exception as e:
                print(f"[restricoes.detail][RUAS][{i}] ERROR: {e}")

        # --- Rios (centerline + faixa) ---
        rios_centerline_feats, rios_faixa_feats = [], []
        rios_qs = list(r.margens_rio.all())
        print(f"[restricoes.detail] margens_rio_count={len(rios_qs)}")
        for i, row in enumerate(rios_qs):
            try:
                if row.centerline:
                    if i < 3: _debug_geom(f"RIO.centerline[{i}]", row.centerline)
                    rios_centerline_feats.append({
                        "type": "Feature",
                        "geometry": json.loads(row.centerline.geojson),
                        "properties": {"margem_m": float(row.margem_m)},
                    })
                if row.faixa:
                    if i < 3: _debug_geom(f"RIO.faixa[{i}]", row.faixa)
                    f = feat(row.faixa, {"margem_m": float(row.margem_m)})
                    if f: rios_faixa_feats.append(f)
            except Exception as e:
                print(f"[restricoes.detail][RIO][{i}] ERROR: {e}")

        # --- LT (centerline + faixa) ---
        lt_centerline_feats, lt_faixa_feats = [], []
        lt_qs = list(r.margens_lt.all())
        print(f"[restricoes.detail] margens_lt_count={len(lt_qs)}")
        for i, row in enumerate(lt_qs):
            try:
                if row.centerline:
                    if i < 3: _debug_geom(f"LT.centerline[{i}]", row.centerline)
                    lt_centerline_feats.append({
                        "type": "Feature",
                        "geometry": json.loads(row.centerline.geojson),
                        "properties": {"margem_m": float(row.margem_m)},
                    })
                if row.faixa:
                    if i < 3: _debug_geom(f"LT.faixa[{i}]", row.faixa)
                    f = feat(row.faixa, {"margem_m": float(row.margem_m)})
                    if f: lt_faixa_feats.append(f)
            except Exception as e:
                print(f"[restricoes.detail][LT][{i}] ERROR: {e}")

        # --- Ferrovias (centerline + faixa) ---
        fer_centerline_feats, fer_faixa_feats = [], []
        fer_qs = list(r.margens_ferrovia.all())
        print(f"[restricoes.detail] margens_ferrovia_count={len(fer_qs)}")
        for i, row in enumerate(fer_qs):
            try:
                if row.centerline:
                    if i < 3: _debug_geom(f"FER.centerline[{i}]", row.centerline)
                    fer_centerline_feats.append({
                        "type": "Feature",
                        "geometry": json.loads(row.centerline.geojson),
                        "properties": {"margem_m": float(row.margem_m)},
                    })
                if row.faixa:
                    if i < 3: _debug_geom(f"FER.faixa[{i}]", row.faixa)
                    f = feat(row.faixa, {"margem_m": float(row.margem_m)})
                    if f: fer_faixa_feats.append(f)
            except Exception as e:
                print(f"[restricoes.detail][FER][{i}] ERROR: {e}")

        # --- resumo final nos logs ---
        print("[restricoes.detail] resumo:",
              f"AV={len(av_feats)}",
              f"CORTES={len(corte_feats)}",
              f"RUAS_eixo={len(rua_eixo_feats)} RUAS_mask={len(rua_mask_feats)}",
              f"RIOS_centerline={len(rios_centerline_feats)} RIOS_faixa={len(rios_faixa_feats)}",
              f"LT_centerline={len(lt_centerline_feats)} LT_faixa={len(lt_faixa_feats)}",
              f"FER_centerline={len(fer_centerline_feats)} FER_faixa={len(fer_faixa_feats)}",
        )

        # --- payload de resposta ---
        data = {
            "restricoes_id": r.id,
            "project_id": r.project_id,
            "version": r.version,
            "label": r.label,
            "notes": r.notes,
            "created_at": r.created_at,
            "srid": SRID_WGS,
            "aoi": aoi_geojson,

            "av": fc(av_feats),
            "corte_av": fc(corte_feats),

            "ruas_eixo": fc(rua_eixo_feats),
            "ruas_mask": fc(rua_mask_feats),

            "rios_centerline": fc(rios_centerline_feats),
            "rios_faixa": fc(rios_faixa_feats),

            "lt_centerline": fc(lt_centerline_feats),
            "lt_faixa": fc(lt_faixa_feats),

            "ferrovias_centerline": fc(fer_centerline_feats),
            "ferrovias_faixa": fc(fer_faixa_feats),
        }
        return Response(data, status=200)
