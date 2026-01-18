# parcelamento/views.py
from __future__ import annotations

import json
import logging

from django.contrib.gis.geos import GEOSGeometry
from django.db import transaction
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from shapely.geometry import shape
from shapely.ops import linemerge, unary_union

from .models import (AreaVazia, Calcada, Lote, ParcelamentoPlano,
                     ParcelamentoVersao, Quarteirao, Via)
from .serializers import (MaterializarRequestSerializer, PlanoSerializer,
                          PreviewRequestSerializer,
                          RecalcularRequestSerializer, VersaoSerializer)
from .services import compute_preview

logger = logging.getLogger(__name__)


class PlanoViewSet(viewsets.ModelViewSet):
    queryset = ParcelamentoPlano.objects.all()
    serializer_class = PlanoSerializer
    permission_classes = [permissions.IsAuthenticated]

    @staticmethod
    def _summ_fc(fc):
        try:
            return len(fc.get("features", []))
        except Exception:
            return 0

    @action(detail=True, methods=["post"])
    def preview(self, request, pk=None):
        """
        Payload:
        {
          "al_geom": <GeoJSON Feature|Geometry WGS84>,
          "params": { ... }
        }

        Retorno (esperado do compute_preview):
        - vias (FC linhas ou eixos)
        - vias_area (FC polígonos)
        - calcadas (FC polígonos)  <-- agora derivadas das vias
        - quarteiroes (FC polígonos)
        - areas_vazias (FC polígonos)
        - lotes (FC - pode vir vazio)
        - metrics (dict)
        """
        req = PreviewRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        data = req.validated_data

        al_geom = data["al_geom"]
        params = data["params"]

        # Normaliza para shapely
        al = shape(al_geom["geometry"] if al_geom.get(
            "type") == "Feature" else al_geom)

        preview = compute_preview(al, params)

        logger.info(
            "[PREVIEW OUT] vias=%d vias_area=%d calcadas=%d quarteiroes=%d areas_vazias=%d lotes=%d",
            self._summ_fc(preview.get("vias", {})),
            self._summ_fc(preview.get("vias_area", {})),
            self._summ_fc(preview.get("calcadas", {})),
            self._summ_fc(preview.get("quarteiroes", {})),
            self._summ_fc(preview.get("areas_vazias", {})),
            self._summ_fc(preview.get("lotes", {})),
        )

        return Response(preview, status=200)

    @action(detail=True, methods=["post"])
    def materializar(self, request, pk=None):
        """
        Mesmo contrato do preview; materializa em versão.

        OBS: materializa a partir do preview calculado (não considera edições do front).
        """
        plano = self.get_object()
        req = MaterializarRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        data = req.validated_data

        al_geom = data["al_geom"]
        params = data["params"]
        nota = data.get("nota", "")
        is_oficial = bool(data.get("is_oficial", False))

        al = shape(al_geom["geometry"] if al_geom.get(
            "type") == "Feature" else al_geom)

        preview = compute_preview(al, params)

        with transaction.atomic():
            versao = ParcelamentoVersao.objects.create(
                plano=plano,
                nota=nota,
                is_oficial=is_oficial,
            )

            # 1) Vias (cria e mantém ordem para linkar calcadas por via_idx)
            vias_criadas: list[Via] = []
            for f in preview.get("vias", {}).get("features", []):
                props = f.get("properties") or {}
                v = Via.objects.create(
                    versao=versao,
                    geom=GEOSGeometry(json.dumps(f["geometry"]), srid=4674),
                    largura_m=float(
                        props.get("largura_m", params.get("larg_rua_vert_m", 8))),
                    tipo=props.get("tipo", "eixo"),
                    categoria=props.get("categoria", "local"),
                    nome=props.get("nome", ""),
                    is_ponte=bool(props.get("is_ponte", False)),
                    ponte_sobre=props.get("ponte_sobre", ""),
                    ia_metadata=props.get("ia_metadata") or {},
                )
                vias_criadas.append(v)

            # 2) Quarteirões
            for f in preview.get("quarteiroes", {}).get("features", []):
                props = f.get("properties") or {}
                Quarteirao.objects.create(
                    versao=versao,
                    geom=GEOSGeometry(json.dumps(f["geometry"]), srid=4674),
                    nome=props.get("nome", ""),
                    numero=int(props.get("numero", 0) or 0),
                    ia_metadata=props.get("ia_metadata") or {},
                )

            # 3) Áreas vazias (resíduos / irregulares)
            for f in preview.get("areas_vazias", {}).get("features", []):
                props = f.get("properties") or {}
                AreaVazia.objects.create(
                    versao=versao,
                    geom=GEOSGeometry(json.dumps(f["geometry"]), srid=4674),
                    motivo=props.get("motivo", "") or "",
                    ia_metadata=props.get("ia_metadata") or {},
                )

            # 4) Calçadas (agora vinculadas à via)
            for f in preview.get("calcadas", {}).get("features", []):
                props = f.get("properties") or {}
                via_obj = None
                via_idx = props.get("via_idx", None)

                if isinstance(via_idx, int) and 0 <= via_idx < len(vias_criadas):
                    via_obj = vias_criadas[via_idx]

                # opcional: lado ("esq"/"dir") via properties ou ia_metadata
                ia_md = props.get("ia_metadata") or {}
                if props.get("lado") and "lado" not in ia_md:
                    ia_md["lado"] = props.get("lado")

                Calcada.objects.create(
                    versao=versao,
                    via=via_obj,
                    geom=GEOSGeometry(json.dumps(f["geometry"]), srid=4674),
                    largura_m=float(
                        props.get("largura_m", params.get("calcada_largura_m", 2.5))),
                    ia_metadata=ia_md,
                )

            # 5) Lotes (ignorado por enquanto, mas mantemos compatibilidade se vier)
            for f in preview.get("lotes", {}).get("features", []):
                props = f.get("properties") or {}
                Lote.objects.create(
                    versao=versao,
                    geom=GEOSGeometry(json.dumps(f["geometry"]), srid=4674),
                    area_m2=float(props.get("area_m2", 0) or 0),
                    frente_m=float(props.get("frente_m", 0) or 0),
                    prof_media_m=float(props.get("prof_media_m", 0) or 0),
                    orientacao_graus=props.get("orientacao_graus", None),
                    score_qualidade=float(
                        props.get("score_qualidade", 0) or 0),
                    frente_min_m=float(params.get("frente_min_m", 0) or 0),
                    prof_min_m=float(params.get("prof_min_m", 0) or 0),
                    numero=int(props.get("numero", 0) or 0),
                    quadra=props.get("quadra", ""),
                    ia_metadata=props.get("ia_metadata") or {},
                )

        return Response({"versao_id": versao.id, "metrics": preview.get("metrics", {})}, status=201)

    @action(detail=True, methods=["post"])
    def recalcular(self, request, pk=None):
        """
        Recalcula propriedades (ex.: área e numeração) a partir de FCs editados no front.
        Payload: { lotes_fc?, vias_fc?, quarteiroes_fc?, calcadas_fc?, renumerar? }

        Mantido por compatibilidade.
        """
        req = RecalcularRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        data = req.validated_data

        # Regras antigas (mantidas):
        lotes_fc = data.get("lotes_fc")
        vias_fc = data.get("vias_fc")
        quarteiroes_fc = data.get("quarteiroes_fc")
        calcadas_fc = data.get("calcadas_fc")
        renumerar = bool(data.get("renumerar", False))

        # Merge eixos (se existir) - mantido para compatibilidade
        def _merge_lines(fc):
            if not fc:
                return None
            lines = []
            for f in fc.get("features", []):
                try:
                    geom = shape(f["geometry"])
                    lines.append(geom)
                except Exception:
                    continue
            if not lines:
                return None
            merged = unary_union(lines)
            try:
                merged = linemerge(merged)
            except Exception:
                pass
            return merged

        _ = _merge_lines(vias_fc)  # placeholder: você pode usar se precisar

        return Response(
            {
                "ok": True,
                "renumerar": renumerar,
                "counts": {
                    "lotes": len((lotes_fc or {}).get("features", [])) if lotes_fc else 0,
                    "vias": len((vias_fc or {}).get("features", [])) if vias_fc else 0,
                    "quarteiroes": len((quarteiroes_fc or {}).get("features", [])) if quarteiroes_fc else 0,
                    "calcadas": len((calcadas_fc or {}).get("features", [])) if calcadas_fc else 0,
                },
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
          - Vias
          - Quarteiroes
          - Calcadas (com via_id e lado)
          - Areas vazias
          - Lotes (se existirem)
        """
        versao = self.get_object()

        vias = [
            {
                "type": "Feature",
                "properties": {
                    "id": v.id,
                    "tipo": v.tipo,
                    "categoria": v.categoria,
                    "nome": v.nome,
                    "largura_m": float(v.largura_m),
                },
                "geometry": json.loads(v.geom.geojson),
            }
            for v in versao.vias.all()
        ]

        quarteiroes = [
            {
                "type": "Feature",
                "properties": {"id": q.id, "numero": q.numero, "nome": q.nome},
                "geometry": json.loads(q.geom.geojson),
            }
            for q in versao.quarteiroes.all()
        ]

        calcadas = [
            {
                "type": "Feature",
                "properties": {
                    "id": c.id,
                    "via_id": c.via_id,
                    "largura_m": float(c.largura_m),
                    "lado": (c.ia_metadata or {}).get("lado"),
                },
                "geometry": json.loads(c.geom.geojson),
            }
            for c in versao.calcadas.all()
        ]

        areas_vazias = [
            {
                "type": "Feature",
                "properties": {"id": a.id, "motivo": a.motivo},
                "geometry": json.loads(a.geom.geojson),
            }
            for a in versao.areas_vazias.all()
        ]

        lotes = [
            {
                "type": "Feature",
                "properties": {
                    "id": l.id,
                    "numero": l.numero,
                    "quadra": l.quadra,
                    "area_m2": float(l.area_m2),
                },
                "geometry": json.loads(l.geom.geojson),
            }
            for l in versao.lotes.all()
        ]

        return Response(
            {
                "vias": {"type": "FeatureCollection", "features": vias},
                "quarteiroes": {"type": "FeatureCollection", "features": quarteiroes},
                "calcadas": {"type": "FeatureCollection", "features": calcadas},
                "areas_vazias": {"type": "FeatureCollection", "features": areas_vazias},
                "lotes": {"type": "FeatureCollection", "features": lotes},
            },
            status=200,
        )

    @action(detail=True, methods=["post"])
    def export_kml(self, request, pk=None):
        """
        Exporta um KML simples (Quarteiroes, Calcadas, Areas Vazias, Lotes).
        """
        versao = self.get_object()

        try:
            import simplekml
        except Exception:
            return Response(
                {"detail": "Dependência simplekml não instalada."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        kml = simplekml.Kml()

        def add_poly(folder, geom_geojson, name_prefix):
            # geom_geojson pode ser Polygon/MultiPolygon
            gtype = geom_geojson.get("type")
            coords = geom_geojson.get("coordinates", [])

            polys = coords if gtype == "MultiPolygon" else [coords]
            for poly in polys:
                if not poly:
                    continue
                outer = poly[0]
                inners = poly[1:] if len(poly) > 1 else []
                pg = folder.newpolygon(name=name_prefix)
                pg.outerboundaryis = outer
                if inners:
                    pg.innerboundaryis = inners

        f_q = kml.newfolder(name="Quarteiroes")
        for q in versao.quarteiroes.all():
            add_poly(f_q, json.loads(q.geom.geojson), f"Q {q.id}")

        f_c = kml.newfolder(name="Calcadas")
        for c in versao.calcadas.all():
            via_label = f" via={c.via_id}" if c.via_id else ""
            lado = (c.ia_metadata or {}).get("lado")
            lado_label = f" lado={lado}" if lado else ""
            add_poly(f_c, json.loads(c.geom.geojson),
                     f"Calcada {c.id}{via_label}{lado_label}")

        f_vz = kml.newfolder(name="Areas Vazias")
        for a in versao.areas_vazias.all():
            motivo = f" ({a.motivo})" if a.motivo else ""
            add_poly(f_vz, json.loads(a.geom.geojson), f"Vazio {a.id}{motivo}")

        f_l = kml.newfolder(name="Lotes")
        for l in versao.lotes.all():
            add_poly(f_l, json.loads(l.geom.geojson),
                     f"Lote {l.id} ({float(l.area_m2)} m2)")

        path = f"/tmp/parcelamento_versao_{versao.id}.kml"
        kml.save(path)
        return Response({"kml_path": path}, status=200)
