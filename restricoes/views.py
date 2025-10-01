from __future__ import annotations
import json
from typing import Any, Dict, Iterable, List

from django.db import transaction, connection, models as djmodels
from django.shortcuts import get_object_or_404
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, MultiLineString
from rest_framework import status, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated

from .models import (
    Restricoes, AreaVerdeV, CorteAreaVerdeV, RuaV,
    MargemRioV, MargemLTV, MargemFerroviaV, SRID_WGS
)
from .serializers import RestricoesSerializer
from projetos.models import Project

SRID_IN = 4326
SRID_WEBMERC = 3857

# ------------- helpers -------------
def _iter(fc: Dict[str, Any]):
    if not fc or fc.get("type") != "FeatureCollection":
        return []
    return fc.get("features", []) or []

def _to_4326(geom: Dict[str, Any]) -> GEOSGeometry:
    g = GEOSGeometry(json.dumps(geom), srid=SRID_IN)
    try:
        if g.srid != SRID_IN:
            g.transform(SRID_IN)
    except Exception:
        g.srid = SRID_IN
    return g

def _as_mpoly(g: GEOSGeometry) -> MultiPolygon:
    if g.geom_type == "Polygon": return MultiPolygon(g)
    if g.geom_type == "MultiPolygon": return g
    raise ValueError("geom precisa ser Polygon/MultiPolygon")

def _as_mline(g: GEOSGeometry) -> MultiLineString:
    if g.geom_type == "LineString": return MultiLineString(g)
    if g.geom_type == "MultiLineString": return g
    raise ValueError("geom precisa ser LineString/MultiLineString")

# --- helpers complementares (coloque perto dos seus helpers existentes) ---
def _get_prop(props: Dict[str, Any], key: str, default: Any = None) -> Any:
    if not isinstance(props, dict):
        return default
    return props.get(key, default)

def _to_wgs(g: GEOSGeometry) -> GEOSGeometry:
    """Garante SRID_WGS (4674) na geometria."""
    if not g:
        return g
    if g.srid != SRID_WGS:
        try:
            g = g.clone()
        except Exception:
            pass
        try:
            g.transform(SRID_WGS)
        except Exception:
            # último recurso: força SRID (só se tiver certeza da origem)
            g.srid = SRID_WGS
    return g

def _buffer_meters(geom: GEOSGeometry, meters: float) -> MultiPolygon:
    """
    Buffer em metros: 4674 -> 3857 (buffer) -> 4674 e retorna MultiPolygon.
    Aceita linhas ou polígonos.
    """
    if not geom or not isinstance(meters, (int, float)) or meters <= 0:
        raise ValueError("buffer_meters: geometria/metros inválidos")

    g = geom.clone()
    try:
        if g.srid != SRID_WEBMERC:
            g.transform(SRID_WEBMERC)
        gb = g.buffer(float(meters))
        if gb.srid != SRID_WGS:
            gb.transform(SRID_WGS)
        # garante multipolygon
        if gb.geom_type == "Polygon":
            return MultiPolygon(gb)
        if gb.geom_type == "MultiPolygon":
            return gb
        # casos raros (ex.: Linestring buffer pode virar geometrycollection em alguns setups)
        # tenta converter via GEOSGeometry.geojson
        gj = GEOSGeometry(gb.geojson, srid=SRID_WGS)
        if gj.geom_type == "Polygon":
            return MultiPolygon(gj)
        if gj.geom_type == "MultiPolygon":
            return gj
        raise ValueError(f"Buffer resultou em tipo inesperado: {gb.geom_type}")
    except Exception as e:
        raise RuntimeError(f"_buffer_meters failed: {e}")


# ------------- POST criar versão -------------
class RestricoesCreateAPIView(APIView):
    """
    POST /projetos/{project_id}/restricoes/

    Payload esperado:
    {
      "label": "vX",
      "notes": "opcional",
      "percent_permitido": 20,
      "corte_pct_cache": 12.5,    // opcional
      "source": "geoman",

      "adHoc": {
        "srid_in": 4326,
        "aoi": { ... Polygon/MultiPolygon ... },                   // OBRIGATÓRIO
        "av": { "type":"FeatureCollection", "features":[...] },    // Poly
        "corte_av": { ... },                                       // Poly
        "ruas": { "features":[{ geometry: Line, properties:{ width_m } }] },
        "rios": { "features":[{ geometry: Line, properties:{ margem_m } }] },
        "lt": { "features":[{ geometry: Line, properties:{ margem_m } }] },
        "ferrovias": { "features":[{ geometry: Line, properties:{ margem_m } }] },
        "default_rua_width": 12,
        "def_margem_rio": 30,
        "def_margem_lt": 15,
        "def_margem_fer": 20
      }
    }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, project_id: int, *args, **kwargs):
        proj = get_object_or_404(Project, pk=project_id)

        # Campos básicos
        label = request.data.get("label", "") or ""
        notes = request.data.get("notes", "") or ""
        percent_permitido = request.data.get("percent_permitido", None)
        corte_pct_cache = request.data.get("corte_pct_cache", None)
        source = request.data.get("source", "geoman")

        # Bloco ad-hoc (geometrias vindas do front)
        ad_hoc = request.data.get("adHoc") or {}
        # srid_in é ignorado pelos seus helpers atuais (que fixam 4326),
        # mas deixamos aqui por compatibilidade futura.
        # srid_in = int(ad_hoc.get("srid_in", 4326))

        # AOI (OBRIGATÓRIA)
        aoi_in = ad_hoc.get("aoi")
        if not aoi_in:
            return Response({"detail": "Campo obrigatório ausente: adHoc.aoi"},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            # _to_4326 => cria com SRID_IN (4326); depois forçamos para 4674
            aoi_4326 = _to_4326(aoi_in)
            aoi_snapshot = _as_mpoly(_to_wgs(aoi_4326))
        except Exception as e:
            return Response({"detail": f"AOI inválida: {e}"}, status=status.HTTP_400_BAD_REQUEST)

        # Collections
        av_fc = ad_hoc.get("av") or {}
        corte_fc = ad_hoc.get("corte_av") or {}
        ruas_fc = ad_hoc.get("ruas") or {}
        rios_fc = ad_hoc.get("rios") or {}
        lt_fc = ad_hoc.get("lt") or {}
        fer_fc = ad_hoc.get("ferrovias") or {}

        # Defaults
        default_rua_width = ad_hoc.get("default_rua_width", 12)
        def_margem_rio = ad_hoc.get("def_margem_rio", 30)
        def_margem_lt = ad_hoc.get("def_margem_lt", 15)
        def_margem_fer = ad_hoc.get("def_margem_fer", 20)

        with transaction.atomic():
            # Cria a versão
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

            # ---- Áreas Verdes (MultiPolygon 4674)
            av_bulk: List[AreaVerdeV] = []
            for feat in _iter(av_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                try:
                    g = _as_mpoly(_to_wgs(_to_4326(geom)))
                except Exception:
                    continue
                av_bulk.append(AreaVerdeV(restricoes=r, geom=g))
            if av_bulk:
                AreaVerdeV.objects.bulk_create(av_bulk, batch_size=500)

            # ---- Cortes de AV (MultiPolygon 4674)
            corte_bulk: List[CorteAreaVerdeV] = []
            for feat in _iter(corte_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                try:
                    g = _as_mpoly(_to_wgs(_to_4326(geom)))
                except Exception:
                    continue
                corte_bulk.append(CorteAreaVerdeV(restricoes=r, geom=g))
            if corte_bulk:
                CorteAreaVerdeV.objects.bulk_create(corte_bulk, batch_size=500)

            # ---- Ruas (MultiLineString 4674) + largura_m
            # Se seu modelo RuaV tiver campo "mask", você pode calcular aqui:
            # mask = _buffer_meters(eixo, largura/2). Se não tiver, só ignore.
            rua_bulk: List[RuaV] = []
            for feat in _iter(ruas_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                props = feat.get("properties") or {}
                largura = _get_prop(props, "width_m", default_rua_width)
                try:
                    eixo = _as_mline(_to_wgs(_to_4326(geom)))
                    try:
                        largura = float(largura)
                    except Exception:
                        largura = float(default_rua_width or 12)
                except Exception:
                    continue

                # Se RuaV possui 'mask' no modelo, calcule e atribua:
                try:
                    # largura é total; máscara é buffer de metade
                    mask = _buffer_meters(eixo, float(largura) / 2.0)
                    rua_bulk.append(RuaV(restricoes=r, eixo=eixo, largura_m=largura, mask=mask))
                except Exception:
                    rua_bulk.append(RuaV(restricoes=r, eixo=eixo, largura_m=largura))
            if rua_bulk:
                RuaV.objects.bulk_create(rua_bulk, batch_size=500)

            # ---- Rios (centerline 4674 + margem_m + faixa bufferizada)
            rio_bulk: List[MargemRioV] = []
            for feat in _iter(rios_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                props = feat.get("properties") or {}
                margem_val = _get_prop(props, "margem_m", def_margem_rio)
                try:
                    line = _as_mline(_to_wgs(_to_4326(geom)))
                    margem = float(margem_val) if margem_val is not None else float(def_margem_rio)
                    faixa = _buffer_meters(line, margem)
                except Exception:
                    continue
                rio_bulk.append(MargemRioV(restricoes=r, centerline=line, margem_m=margem, faixa=faixa))
            if rio_bulk:
                MargemRioV.objects.bulk_create(rio_bulk, batch_size=500)

            # ---- Linhas de Transmissão
            lt_bulk: List[MargemLTV] = []
            for feat in _iter(lt_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                props = feat.get("properties") or {}
                margem_val = _get_prop(props, "margem_m", def_margem_lt)
                try:
                    line = _as_mline(_to_wgs(_to_4326(geom)))
                    margem = float(margem_val) if margem_val is not None else float(def_margem_lt)
                    faixa = _buffer_meters(line, margem)
                except Exception:
                    continue
                lt_bulk.append(MargemLTV(restricoes=r, centerline=line, margem_m=margem, faixa=faixa))
            if lt_bulk:
                MargemLTV.objects.bulk_create(lt_bulk, batch_size=500)

            # ---- Ferrovias
            fer_bulk: List[MargemFerroviaV] = []
            for feat in _iter(fer_fc):
                geom = feat.get("geometry")
                if not geom:
                    continue
                props = feat.get("properties") or {}
                margem_val = _get_prop(props, "margem_m", def_margem_fer)
                try:
                    line = _as_mline(_to_wgs(_to_4326(geom)))
                    margem = float(margem_val) if margem_val is not None else float(def_margem_fer)
                    faixa = _buffer_meters(line, margem)
                except Exception:
                    continue
                fer_bulk.append(MargemFerroviaV(restricoes=r, centerline=line, margem_m=margem, faixa=faixa))
            if fer_bulk:
                MargemFerroviaV.objects.bulk_create(fer_bulk, batch_size=500)

            # ---- Resumo p/ resposta
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

# ------------- LISTAR versões por projeto -------------
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

# ------------- DETALHE (GeoJSON p/ reabrir no Geoman) -------------
class RestricoesGeoDetailAPIView(APIView):
    permission_classes = [IsAuthenticated]
    

    def get(self, request, restricoes_id: int, *args, **kwargs):
        r = get_object_or_404(Restricoes, pk=restricoes_id)

        def fc(features): return {"type":"FeatureCollection","features":features}
        def feat(g, props=None): return {"type":"Feature","geometry": json.loads(g.geojson), "properties": props or {}}

        av_feats = [feat(row.geom) for row in r.areas_verdes.all()]
        corte_feats = [feat(row.geom) for row in r.cortes_av.all()]

        rua_feats = []
        for row in r.ruas.all():
            try:
                rua_feats.append({"type":"Feature","geometry": json.loads(row.eixo.geojson),"properties":{"width_m": row.largura_m}})
            except: pass

        rua_mask_feats = []
        for row in r.ruas.all():
            try:
                if row.mask:
                    rua_mask_feats.append(feat(row.mask, {"width_m": row.largura_m}))
            except: pass

        rios_faixa_feats = [feat(row.faixa, {"margem_m": row.margem_m}) for row in r.margens_rio.all() if row.faixa]
        lt_faixa_feats  = [feat(row.faixa, {"margem_m": row.margem_m}) for row in r.margens_lt.all() if row.faixa]
        fer_faixa_feats = [feat(row.faixa, {"margem_m": row.margem_m}) for row in r.margens_ferrovia.all() if row.faixa]

        data = {
            "restricoes_id": r.id,
            "project_id": r.project_id,
            "version": r.version,
            "label": r.label,
            "notes": r.notes,
            "created_at": r.created_at,
            "srid": SRID_WGS,
            "av": fc(av_feats),
            "corte_av": fc(corte_feats),
            "ruas": fc(rua_feats),
            "rua_mask": fc(rua_mask_feats),
            "rios_faixa": fc(rios_faixa_feats),
            "lt_faixa": fc(lt_faixa_feats),
            "ferrovias_faixa": fc(fer_faixa_feats),
            "aoi": json.loads(r.aoi_snapshot.geojson) if r.aoi_snapshot else None,
        }
        return Response(data, status=status.HTTP_200_OK)
