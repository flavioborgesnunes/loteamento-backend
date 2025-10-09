from rest_framework import status, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db import transaction
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, LineString, Polygon
from .models import ParcelamentoPlano, ParcelamentoVersao, Via, Quarteirao, Lote
from .serializers import (
    PlanoSerializer, VersaoSerializer, ViaSerializer, QuarteiraoSerializer, LoteSerializer,
    PreviewRequestSerializer, PreviewResponseSerializer, MaterializarRequestSerializer
)
from .services import compute_preview
from django.core.exceptions import ObjectDoesNotExist
import json

class PlanoViewSet(viewsets.ModelViewSet):
    queryset = ParcelamentoPlano.objects.all()
    serializer_class = PlanoSerializer
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=True, methods=["post"])
    def preview(self, request, pk=None):
        plano = self.get_object()
        req = PreviewRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        al = req.validated_data["al_geom"]
        params = req.validated_data["params"]
        # fallback: preencher com defaults do plano
        for k in ["frente_min_m","prof_min_m","larg_rua_vert_m","larg_rua_horiz_m","compr_max_quarteirao_m","srid_calc"]:
            params[k] = params.get(k, getattr(plano, k))
        if params.get("orientacao_graus") is None and plano.orientacao_graus is not None:
            params["orientacao_graus"] = float(plano.orientacao_graus)

        preview = compute_preview(al, params)
        return Response(preview, status=200)

    @action(detail=True, methods=["post"])
    def materializar(self, request, pk=None):
        plano = self.get_object()
        req = MaterializarRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        al = req.validated_data["al_geom"]
        params = req.validated_data["params"]
        nota = req.validated_data.get("nota","")
        is_oficial = req.validated_data.get("is_oficial", False)
        for k in ["frente_min_m","prof_min_m","larg_rua_vert_m","larg_rua_horiz_m","compr_max_quarteirao_m","srid_calc"]:
            params[k] = params.get(k, getattr(plano, k))
        if params.get("orientacao_graus") is None and plano.orientacao_graus is not None:
            params["orientacao_graus"] = float(plano.orientacao_graus)

        preview = compute_preview(al, params)

        with transaction.atomic():
            versao = ParcelamentoVersao.objects.create(
                plano=plano, is_oficial=is_oficial, nota=nota,
                frente_min_m=params["frente_min_m"], prof_min_m=params["prof_min_m"],
                larg_rua_vert_m=params["larg_rua_vert_m"], larg_rua_horiz_m=params["larg_rua_horiz_m"],
                compr_max_quarteirao_m=params["compr_max_quarteirao_m"],
                orientacao_graus=params.get("orientacao_graus"),
                srid_calc=params.get("srid_calc", 3857)
            )
            # Persistir vias
            for f in preview["vias"]["features"]:
                Via.objects.create(
                    versao=versao,
                    geom=GEOSGeometry(str(f["geometry"])),
                    largura_m=f["properties"].get("largura_m", params["larg_rua_vert_m"]),
                    tipo=f["properties"].get("tipo","vertical")
                )
            # Quarteiroes
            for f in preview["quarteiroes"]["features"]:
                Quarteirao.objects.create(
                    versao=versao,
                    geom=GEOSGeometry(str(f["geometry"]))
                )
            # Lotes (metrificação mínima; cálculos finos podem ir em triggers)
            for f in preview["lotes"]["features"]:
                Lote.objects.create(
                    versao=versao,
                    geom=GEOSGeometry(str(f["geometry"])),
                    area_m2=f["properties"].get("area_m2", 0),
                    frente_m=f["properties"].get("frente_aprox_m", 0),
                    prof_media_m=f["properties"].get("prof_aprox_m", 0),
                    score_qualidade=f["properties"].get("score_qualidade", 0),
                    frente_min_m=params["frente_min_m"],
                    prof_min_m=params["prof_min_m"],
                )
        return Response({"versao_id": versao.id, "metrics": preview["metrics"]}, status=201)


class VersaoViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ParcelamentoVersao.objects.all()
    serializer_class = VersaoSerializer
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=True, methods=["get"])
    def geojson(self, request, pk=None):
        versao = self.get_object()
        vias = [{"type":"Feature","properties":{"id":v.id,"tipo":v.tipo,"largura_m":float(v.largura_m)},
                 "geometry":v.geom.geojson and v.geom.json}]
        # O Django GEOS não dá .json direto em todas versões; usar str(v.geom)
        vias = [{
            "type":"Feature",
            "properties":{"id":v.id,"tipo":v.tipo,"largura_m":float(v.largura_m)},
            "geometry": json.loads(v.geom.geojson)
        } for v in versao.vias.all()]

        quarts = [{
            "type":"Feature",
            "properties":{"id":q.id},
            "geometry": json.loads(q.geom.geojson)
        } for q in versao.quarteiroes.all()]

        lotes = [{
            "type":"Feature",
            "properties":{
                "id":l.id,"area_m2":float(l.area_m2),"frente_m":float(l.frente_m),
                "prof_media_m":float(l.prof_media_m),"score_qualidade":float(l.score_qualidade)
            },
            "geometry": json.loads(l.geom.geojson)
        } for l in versao.lotes.all()]

        return Response({
            "vias":{"type":"FeatureCollection","features":vias},
            "quarteiroes":{"type":"FeatureCollection","features":quarts},
            "lotes":{"type":"FeatureCollection","features":lotes},
        }, status=200)

    @action(detail=True, methods=["post"])
    def kml(self, request, pk=None):
        versao = self.get_object()
        try:
            import simplekml
        except ImportError:
            return Response({"detail":"Instale simplekml (pip install simplekml)"}, status=400)

        kml = simplekml.Kml()
        f_vias = kml.newfolder(name="Vias")
        for v in versao.vias.all():
            ls = f_vias.newlinestring(name=f"Via {v.id}")
            ls.coords = list(v.geom.coords)
            ls.style.linestyle.width = 3

        f_q = kml.newfolder(name="Quarteiroes")
        for q in versao.quarteiroes.all():
            for poly in q.geom:
                pg = f_q.newpolygon(name=f"Q {q.id}")
                pg.outerboundaryis = list(poly.exterior.coords)
                for i in poly.interiors:
                    pg.innerboundaryis = [list(i.coords)]

        f_l = kml.newfolder(name="Lotes")
        for l in versao.lotes.all():
            for poly in l.geom:
                pg = f_l.newpolygon(name=f"Lote {l.id} ({l.area_m2} m2)")
                pg.outerboundaryis = list(poly.exterior.coords)
                for i in poly.interiors:
                    pg.innerboundaryis = [list(i.coords)]

        path = f"/tmp/parcelamento_versao_{versao.id}.kml"
        kml.save(path)
        # você pode devolver como arquivo; aqui devolvo um “link” simples:
        return Response({"kml_path": path}, status=200)
