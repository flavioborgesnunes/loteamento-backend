import json

from django.contrib.gis.geos import GEOSGeometry
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from shapely.geometry import mapping, shape
from shapely.ops import linemerge

from .models import (Lote, ParcelamentoPlano, ParcelamentoVersao, Quarteirao,
                     Via)
from .serializers import (LoteSerializer, MaterializarRequestSerializer,
                          PlanoSerializer, PreviewRequestSerializer,
                          PreviewResponseSerializer, QuarteiraoSerializer,
                          VersaoSerializer, ViaSerializer)
from .services import compute_preview


class PlanoViewSet(viewsets.ModelViewSet):
    queryset = ParcelamentoPlano.objects.all()
    serializer_class = PlanoSerializer
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=True, methods=["post"])
    def preview(self, request, pk=None):
        """
        Espera body com:
        {
          "al_geom": <Feature/Geometry WGS84>,
          "params": {
              // numéricos do plano (fallback se ausentes):
              "frente_min_m", "prof_min_m",
              "larg_rua_vert_m", "larg_rua_horiz_m",
              "compr_max_quarteirao_m", "srid_calc",
              "orientacao_graus" (opcional),

              // ---- NOVOS OPCIONAIS DE ORIENTAÇÃO/REGRAS ----
              "ruas_mask_fc": <FeatureCollection>,
              "ruas_eixo_fc": <FeatureCollection>,
              "guia_linha_fc": <FeatureCollection>,
              "dist_min_rua_quarteirao_m": <float>
          }
        }
        """
        plano = self.get_object()
        req = PreviewRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        al = req.validated_data["al_geom"]
        params = req.validated_data["params"] or {}

        # defaults do plano
        for k in ["frente_min_m", "prof_min_m", "larg_rua_vert_m", "larg_rua_horiz_m", "compr_max_quarteirao_m", "srid_calc"]:
            params[k] = params.get(k, getattr(plano, k))
        if params.get("orientacao_graus") is None and plano.orientacao_graus is not None:
            params["orientacao_graus"] = float(plano.orientacao_graus)

        # repassamos params como veio (inclui os novos campos se presentes)
        preview = compute_preview(al, params)
        return Response(preview, status=200)

    @action(detail=True, methods=["post"])
    def materializar(self, request, pk=None):
        """
        Mesmo contrato do preview; materializa em versão.
        """
        plano = self.get_object()
        req = MaterializarRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        al = req.validated_data["al_geom"]
        params = req.validated_data["params"] or {}
        nota = req.validated_data.get("nota", "")
        is_oficial = req.validated_data.get("is_oficial", False)

        # defaults do plano
        for k in ["frente_min_m", "prof_min_m", "larg_rua_vert_m", "larg_rua_horiz_m", "compr_max_quarteirao_m", "srid_calc"]:
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

            # Vias
            for f in preview["vias"]["features"]:
                Via.objects.create(
                    versao=versao,
                    geom=GEOSGeometry(json.dumps(f["geometry"]), srid=4326),
                    largura_m=float(f["properties"].get(
                        "largura_m", params["larg_rua_vert_m"])),
                    tipo=f["properties"].get("tipo", "vertical"),
                )

            # Quarteirões
            for f in preview["quarteiroes"]["features"]:
                Quarteirao.objects.create(
                    versao=versao,
                    geom=GEOSGeometry(json.dumps(f["geometry"]), srid=4326),
                )

            # Lotes
            for f in preview["lotes"]["features"]:
                Lote.objects.create(
                    versao=versao,
                    geom=GEOSGeometry(json.dumps(f["geometry"]), srid=4326),
                    area_m2=float(f["properties"].get("area_m2", 0)),
                    frente_m=float(f["properties"].get("frente_aprox_m", 0)),
                    prof_media_m=float(f["properties"].get("prof_aprox_m", 0)),
                    score_qualidade=float(
                        f["properties"].get("score_qualidade", 0)),
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

        vias = [{
            "type": "Feature",
            "properties": {"id": v.id, "tipo": v.tipo, "largura_m": float(v.largura_m)},
            "geometry": json.loads(v.geom.geojson)
        } for v in versao.vias.all()]

        quarts = [{
            "type": "Feature",
            "properties": {"id": q.id},
            "geometry": json.loads(q.geom.geojson)
        } for q in versao.quarteiroes.all()]

        lotes = [{
            "type": "Feature",
            "properties": {
                "id": l.id, "area_m2": float(l.area_m2), "frente_m": float(l.frente_m),
                "prof_media_m": float(l.prof_media_m), "score_qualidade": float(l.score_qualidade)
            },
            "geometry": json.loads(l.geom.geojson)
        } for l in versao.lotes.all()]

        return Response({
            "vias": {"type": "FeatureCollection", "features": vias},
            "quarteiroes": {"type": "FeatureCollection", "features": quarts},
            "lotes": {"type": "FeatureCollection", "features": lotes},
        }, status=200)

    @action(detail=True, methods=["get"])
    def geojson_com_bordas(self, request, pk=None):
        versao = self.get_object()
        lotes_lin = []
        for l in versao.lotes.all():
            shp_poly = shape(json.loads(l.geom.geojson))
            border = shp_poly.boundary
            try:
                border = linemerge(border)
            except Exception:
                pass
            lotes_lin.append({
                "type": "Feature",
                "properties": {"id": l.id},
                "geometry": mapping(border)
            })
        return Response({"type": "FeatureCollection", "features": lotes_lin}, status=200)

    @action(detail=True, methods=["post"])
    def kml(self, request, pk=None):
        versao = self.get_object()
        try:
            import simplekml
        except ImportError:
            return Response({"detail": "Instale simplekml (pip install simplekml)"}, status=400)

        kml = simplekml.Kml()

        # Vias
        f_vias = kml.newfolder(name="Vias")
        for v in versao.vias.all():
            gj = json.loads(v.geom.geojson)

            def add_line(g):
                if g["type"] == "LineString":
                    ls = f_vias.newlinestring(name=f"Via {v.id}")
                    ls.coords = g["coordinates"]
                    ls.style.linestyle.width = 3
                elif g["type"] == "MultiLineString":
                    for coords in g["coordinates"]:
                        ls = f_vias.newlinestring(name=f"Via {v.id}")
                        ls.coords = coords
                        ls.style.linestyle.width = 3

            add_line(gj)

        def add_poly(folder, g, name_prefix):
            if g["type"] == "Polygon":
                polys = [g["coordinates"]]
            elif g["type"] == "MultiPolygon":
                polys = g["coordinates"]
            else:
                return
            for coords in polys:
                outer = coords[0]
                inners = coords[1:] if len(coords) > 1 else []
                pg = folder.newpolygon(name=name_prefix)
                pg.outerboundaryis = outer
                if inners:
                    pg.innerboundaryis = inners

        f_q = kml.newfolder(name="Quarteiroes")
        for q in versao.quarteiroes.all():
            add_poly(f_q, json.loads(q.geom.geojson), f"Q {q.id}")

        f_l = kml.newfolder(name="Lotes")
        for l in versao.lotes.all():
            add_poly(f_l, json.loads(l.geom.geojson),
                     f"Lote {l.id} ({l.area_m2} m2)")

        path = f"/tmp/parcelamento_versao_{versao.id}.kml"
        kml.save(path)
        return Response({"kml_path": path}, status=200)
