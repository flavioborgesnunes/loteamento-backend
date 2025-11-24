# parcelamento/views.py
from __future__ import annotations

import json
import logging

from django.contrib.gis.geos import GEOSGeometry
from django.db import transaction
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from shapely.geometry import mapping, shape
from shapely.ops import linemerge, unary_union

from .models import (AreaPublica, Calcada, Lote, ParcelamentoPlano,
                     ParcelamentoVersao, Quarteirao, Via)
from .serializers import (LoteSerializer, MaterializarRequestSerializer,
                          PlanoSerializer, PreviewRequestSerializer,
                          PreviewResponseSerializer, QuarteiraoSerializer,
                          RecalcularRequestSerializer, VersaoSerializer,
                          ViaSerializer)
from .services import compute_preview

logger = logging.getLogger(__name__)


class PlanoViewSet(viewsets.ModelViewSet):
    queryset = ParcelamentoPlano.objects.all()
    serializer_class = PlanoSerializer
    permission_classes = [permissions.IsAuthenticated]

    @staticmethod
    def _summ_fc(fc):
        try:
            return dict(
                ok=isinstance(fc, dict) and fc.get(
                    "type") == "FeatureCollection",
                n=len(fc.get("features", [])) if isinstance(
                    fc, dict) else None,
                g0=(fc.get("features", [{}])[0].get(
                    "geometry", {}) or {}).get("type"),
            )
        except Exception:
            return {"ok": False, "n": None, "g0": None}

    @action(detail=True, methods=["post"])
    def preview(self, request, pk=None):
        """
        Espera body com:
        {
          "al_geom": <Feature/Geometry WGS84>,
          "params": {
              "frente_min_m", "prof_min_m",
              "larg_rua_vert_m", "larg_rua_horiz_m",
              "compr_max_quarteirao_m", "srid_calc",
              "orientacao_graus" (opcional),

              // opcionais:
              "ruas_mask_fc", "ruas_eixo_fc",
              "has_ruas_mask_fc", "has_ruas_eixo_fc",
              "guia_linha_fc", "dist_min_rua_quarteirao_m",
              "tolerancia_frac", "calcada_largura_m"
          }
        }
        """
        plano = self.get_object()
        req = PreviewRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)

        al = req.validated_data["al_geom"]
        params = req.validated_data["params"] or {}

        # defaults do plano
        for k in [
            "frente_min_m",
            "prof_min_m",
            "larg_rua_vert_m",
            "larg_rua_horiz_m",
            "compr_max_quarteirao_m",
            "srid_calc",
        ]:
            params[k] = params.get(k, getattr(plano, k))
        if params.get("orientacao_graus") is None and plano.orientacao_graus is not None:
            params["orientacao_graus"] = float(plano.orientacao_graus)

        logger.info("[PREVIEW IN] params=%s", list(params.keys()))
        logger.info(
            "[PREVIEW IN] ruas_mask_fc=%s",
            self._summ_fc(params.get("ruas_mask_fc")),
        )
        logger.info(
            "[PREVIEW IN] ruas_eixo_fc=%s",
            self._summ_fc(params.get("ruas_eixo_fc")),
        )

        preview = compute_preview(al, params)
        # preview inclui: vias, quarteiroes, lotes, calcadas, vias_area, areas_publicas, metrics

        logger.info(
            "[PREVIEW OUT] vias=%d quarteiroes=%d lotes=%d calcadas=%d vias_area=%d areas_publicas=%d",
            len(preview["vias"]["features"]),
            len(preview["quarteiroes"]["features"]),
            len(preview["lotes"]["features"]),
            len(preview.get("calcadas", {}).get("features", [])),
            len(preview.get("vias_area", {}).get("features", [])),
            len(preview.get("areas_publicas", {}).get("features", [])),
        )
        return Response(preview, status=200)

    @action(detail=True, methods=["post"])
    def materializar(self, request, pk=None):
        """
        Mesmo contrato do preview; materializa em versão.

        OBS importante: hoje ele materializa a partir do preview calculado
        (sem considerar edições feitas no front). Depois podemos criar um
        endpoint separado para "salvar versão a partir dos FC editados".
        """
        plano = self.get_object()
        req = MaterializarRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)

        al = req.validated_data["al_geom"]
        params = req.validated_data["params"] or {}
        nota = req.validated_data.get("nota", "")
        is_oficial = req.validated_data.get("is_oficial", False)

        # defaults do plano
        for k in [
            "frente_min_m",
            "prof_min_m",
            "larg_rua_vert_m",
            "larg_rua_horiz_m",
            "compr_max_quarteirao_m",
            "srid_calc",
        ]:
            params[k] = params.get(k, getattr(plano, k))
        if params.get("orientacao_graus") is None and plano.orientacao_graus is not None:
            params["orientacao_graus"] = float(plano.orientacao_graus)

        preview = compute_preview(al, params)

        with transaction.atomic():
            versao = ParcelamentoVersao.objects.create(
                plano=plano,
                is_oficial=is_oficial,
                nota=nota,
                frente_min_m=params["frente_min_m"],
                prof_min_m=params["prof_min_m"],
                larg_rua_vert_m=params["larg_rua_vert_m"],
                larg_rua_horiz_m=params["larg_rua_horiz_m"],
                compr_max_quarteirao_m=params["compr_max_quarteirao_m"],
                orientacao_graus=params.get("orientacao_graus"),
                srid_calc=params.get("srid_calc", 3857),
            )

            # Vias
            for f in preview["vias"]["features"]:
                props = f.get("properties") or {}
                Via.objects.create(
                    versao=versao,
                    geom=GEOSGeometry(json.dumps(f["geometry"]), srid=4326),
                    largura_m=float(
                        props.get("largura_m", params["larg_rua_vert_m"])
                    ),
                    tipo=props.get("tipo", "vertical"),
                    categoria=props.get("categoria", "local"),
                    nome=props.get("nome", ""),
                    is_ponte=bool(props.get("is_ponte", False)),
                    ponte_sobre=props.get("ponte_sobre", ""),
                )

            # Quarteirões
            for f in preview["quarteiroes"]["features"]:
                Quarteirao.objects.create(
                    versao=versao,
                    geom=GEOSGeometry(json.dumps(f["geometry"]), srid=4326),
                )

            # Calçadas (se vierem no preview)
            for f in preview.get("calcadas", {}).get("features", []):
                props = f.get("properties") or {}
                Calcada.objects.create(
                    versao=versao,
                    geom=GEOSGeometry(json.dumps(f["geometry"]), srid=4326),
                    largura_m=float(props.get("largura_m", 2.5)),
                )

            # Áreas públicas (se vierem no preview — hoje o heurístico manda vazio,
            # mas a IA pode preencher isso no futuro)
            for f in preview.get("areas_publicas", {}).get("features", []):
                props = f.get("properties") or {}
                AreaPublica.objects.create(
                    versao=versao,
                    geom=GEOSGeometry(json.dumps(f["geometry"]), srid=4326),
                    tipo=props.get("tipo", "praca"),
                    nome=props.get("nome", ""),
                    descricao=props.get("descricao", ""),
                )

            # Lotes
            for f in preview["lotes"]["features"]:
                props = f.get("properties") or {}
                Lote.objects.create(
                    versao=versao,
                    geom=GEOSGeometry(json.dumps(f["geometry"]), srid=4326),
                    area_m2=float(props.get("area_m2", 0)),
                    frente_m=float(props.get("frente_m", 0)),  # corrigido
                    prof_media_m=float(
                        props.get("prof_media_m", 0)),  # corrigido
                    orientacao_graus=props.get("orientacao_graus"),
                    score_qualidade=float(props.get("score_qualidade", 0)),
                    frente_min_m=params["frente_min_m"],
                    prof_min_m=params["prof_min_m"],
                    numero=int(props.get("numero", 0)),
                    quadra=props.get("quadra", ""),
                )

        return Response(
            {"versao_id": versao.id, "metrics": preview["metrics"]},
            status=201,
        )

    @action(detail=True, methods=["post"])
    def recalcular(self, request, pk=None):
        """
        Recalcula propriedades (ex.: área e numeração) a partir de FCs editados no front.
        Payload: { lotes_fc?, vias_fc?, quarteiroes_fc?, calcadas_fc?, renumerar? }
        """
        req = RecalcularRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)

        from pyproj import Transformer
        from shapely.geometry import Point as ShPoint
        from shapely.ops import transform as shp_transform

        def shapely_transform(geom, transformer: Transformer):
            def _tx(x, y, z=None):
                x2, y2 = transformer.transform(x, y)
                return (x2, y2) if z is None else (x2, y2, z)

            return shp_transform(_tx, geom)

        lotes_fc = req.validated_data.get("lotes_fc") or {
            "type": "FeatureCollection",
            "features": [],
        }
        renum = req.validated_data.get("renumerar", True)

        srid_calc = int(request.data.get("srid_calc", 3857))
        tf_wgs_to_m = Transformer.from_crs(4326, srid_calc, always_xy=True)
        tf_m_to_wgs = Transformer.from_crs(srid_calc, 4326, always_xy=True)

        out_features = []
        lot_num = 1
        for f in lotes_fc.get("features", []):
            try:
                g_wgs = shape(f.get("geometry"))
                g_m = shapely_transform(g_wgs, tf_wgs_to_m)
                if g_m.is_empty:
                    continue
                area_m2 = float(abs(g_m.area))
                props = dict(f.get("properties") or {})
                if renum:
                    # mantém compatibilidade com o nome antigo e o novo
                    props["lot_number"] = lot_num
                    props["numero"] = lot_num
                    lot_num += 1
                props["area_m2"] = round(area_m2, 2)

                # se não houver labels, gerar labels básicas
                if "label_center" not in props:
                    c = g_m.representative_point()
                    cw = shapely_transform(c, tf_m_to_wgs)
                    props["label_center"] = [cw.x, cw.y]
                if "label_corner" not in props:
                    # primeiro vértice externo
                    cx, cy = list(g_m.exterior.coords)[0]
                    corner_w = shapely_transform(ShPoint(cx, cy), tf_m_to_wgs)
                    props["label_corner"] = [corner_w.x, corner_w.y]

                out_features.append(
                    {
                        "type": "Feature",
                        "properties": props,
                        "geometry": f["geometry"],
                    }
                )
            except Exception:
                continue

        return Response(
            {
                "lotes": {
                    "type": "FeatureCollection",
                    "features": out_features,
                }
            },
            status=200,
        )


class VersaoViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ParcelamentoVersao.objects.all()
    serializer_class = VersaoSerializer
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=True, methods=["get"])
    def geojson(self, request, pk=None):
        """
        Devolve:
          - Vias (linhas)
          - Quarteiroes
          - Lotes
          - Calcadas
          - vias_area (áreas cinza)
          - areas_publicas
        """
        versao = self.get_object()

        vias = [
            {
                "type": "Feature",
                "properties": {
                    "id": v.id,
                    "tipo": v.tipo,
                    "largura_m": float(v.largura_m),
                    "categoria": v.categoria,
                    "nome": v.nome,
                    "is_ponte": v.is_ponte,
                    "ponte_sobre": v.ponte_sobre,
                },
                "geometry": json.loads(v.geom.geojson),
            }
            for v in versao.vias.all()
        ]

        quarts = [
            {
                "type": "Feature",
                "properties": {"id": q.id},
                "geometry": json.loads(q.geom.geojson),
            }
            for q in versao.quarteiroes.all()
        ]

        lotes = [
            {
                "type": "Feature",
                "properties": {
                    "id": l.id,
                    "area_m2": float(l.area_m2),
                    "frente_m": float(l.frente_m),
                    "prof_media_m": float(l.prof_media_m),
                    "score_qualidade": float(l.score_qualidade),
                    "orientacao_graus": float(l.orientacao_graus)
                    if l.orientacao_graus is not None
                    else None,
                    "frente_min_m": float(l.frente_min_m),
                    "prof_min_m": float(l.prof_min_m),
                    "numero": l.numero,
                    "quadra": l.quadra,
                },
                "geometry": json.loads(l.geom.geojson),
            }
            for l in versao.lotes.all()
        ]

        calcadas = [
            {
                "type": "Feature",
                "properties": {
                    "id": c.id,
                    "largura_m": float(c.largura_m),
                },
                "geometry": json.loads(c.geom.geojson),
            }
            for c in versao.calcadas.all()
        ]

        areas_publicas = [
            {
                "type": "Feature",
                "properties": {
                    "id": a.id,
                    "tipo": a.tipo,
                    "nome": a.nome,
                    "descricao": a.descricao,
                },
                "geometry": json.loads(a.geom.geojson),
            }
            for a in versao.areas_publicas.all()
        ]

        # --------- vias_area a partir do buffer dos eixos ---------
        try:
            from shapely.geometry import shape as shp_shape
            from shapely.ops import unary_union

            buffers = []
            for v in versao.vias.all():
                gj = json.loads(v.geom.geojson)
                s = shp_shape(gj)
                if s.is_empty:
                    continue
                width = float(v.largura_m or 0)
                half = max(width, 0.0) / 2.0
                # buffer em graus (ok pra visual; seus dados são WGS84)
                b = s.buffer(half, cap_style=2, join_style=2)
                if not b.is_empty:
                    buffers.append(b)
            ua = unary_union(buffers) if buffers else None
            vias_area = []
            if ua and not ua.is_empty:
                def _to_geo(g):
                    return {
                        "type": "Feature",
                        "properties": {},
                        "geometry": mapping(g),
                    }

                if hasattr(ua, "geoms"):
                    vias_area = [_to_geo(g) for g in ua.geoms]
                else:
                    vias_area = [_to_geo(ua)]
            vias_area_fc = {"type": "FeatureCollection", "features": vias_area}
        except Exception:
            vias_area_fc = {"type": "FeatureCollection", "features": []}

        return Response(
            {
                "vias": {"type": "FeatureCollection", "features": vias},
                "quarteiroes": {"type": "FeatureCollection", "features": quarts},
                "lotes": {"type": "FeatureCollection", "features": lotes},
                "calcadas": {"type": "FeatureCollection", "features": calcadas},
                "vias_area": vias_area_fc,
                "areas_publicas": {
                    "type": "FeatureCollection",
                    "features": areas_publicas,
                },
            },
            status=200,
        )

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
            lotes_lin.append(
                {
                    "type": "Feature",
                    "properties": {"id": l.id},
                    "geometry": mapping(border),
                }
            )
        return Response(
            {"type": "FeatureCollection", "features": lotes_lin},
            status=200,
        )

    @action(detail=True, methods=["post"])
    def kml(self, request, pk=None):
        versao = self.get_object()
        try:
            import simplekml
        except ImportError:
            return Response(
                {"detail": "Instale simplekml (pip install simplekml)"},
                status=400,
            )

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

        f_c = kml.newfolder(name="Calcadas")
        for c in versao.calcadas.all():
            add_poly(f_c, json.loads(c.geom.geojson), f"Calcada {c.id}")

        f_l = kml.newfolder(name="Lotes")
        for l in versao.lotes.all():
            add_poly(
                f_l,
                json.loads(l.geom.geojson),
                f"Lote {l.id} ({l.area_m2} m2)",
            )

        # (opcional) poderíamos adicionar Áreas Públicas no KML também
        f_ap = kml.newfolder(name="Areas Publicas")
        for a in versao.areas_publicas.all():
            add_poly(
                f_ap,
                json.loads(a.geom.geojson),
                f"AreaPublica {a.id} ({a.get_tipo_display()})",
            )

        path = f"/tmp/parcelamento_versao_{versao.id}.kml"
        kml.save(path)
        return Response({"kml_path": path}, status=200)
