from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Set

from django.contrib.gis.db.models import GeometryField
from django.contrib.gis.db.models.functions import Intersection, MakeValid
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, WKBWriter
from django.core.files.base import ContentFile
from django.db import connection, transaction
from django.db.models import F, Func, Value
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from geodata.models import (Area, Cidade, LimiteFederal, LinhaTransmissao,
                            MalhaFerroviaria)
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rios.models import Waterway

from .models import ExportSnapshot, MapArtifact, Project, ProjectFeature
from .serializers import ProjectSerializer, ProjectUpsertExportSerializer
from .utils import build_kmz_from_payload

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------


def _force2d_now(g):
    """Força 2D AGORA (independente do que veio antes)."""
    if not g:
        return g
    try:
        if getattr(g, "hasz", False):
            w = WKBWriter()
            w.outdim = 2
            return GEOSGeometry(w.write(g), srid=g.srid or 4326)
    except Exception:
        return GEOSGeometry(g.wkt, srid=g.srid or 4326)
    return g


def _geos_force2d(g: Optional[GEOSGeometry]) -> Optional[GEOSGeometry]:
    if not g:
        return g
    try:
        if getattr(g, "hasz", False):
            w = WKBWriter()
            w.outdim = 2
            return GEOSGeometry(w.write(g), srid=g.srid or 4326)
    except Exception:
        return GEOSGeometry(g.wkt, srid=g.srid or 4326)
    return g


def _yield_ids_in_batches(qs_ids, batch_size=2000):
    batch = []
    for _id in qs_ids:
        batch.append(_id)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _same_tenant_or_owner(user, project: Project) -> bool:
    try:
        if getattr(user, "role", None) == "dono":
            return project.dono_id == getattr(user, "id", None)
        if project.owner_id == getattr(user, "id", None):
            return True
        if getattr(user, "dono", None):
            return project.dono_id == getattr(user, "dono", None)
        return project.owner_id == getattr(user, "id", None)
    except Exception:
        return project.owner_id == getattr(user, "id", None)


def _ensure_mp(g: GEOSGeometry) -> GEOSGeometry:
    if g.geom_type == "Polygon":
        return MultiPolygon([g], srid=g.srid or 4326)
    if g.geom_type == "MultiPolygon":
        return g
    raise TypeError(
        f"AOI deve ser Polygon ou MultiPolygon; veio {g.geom_type}")


def _force2d_all_geom_fields(instance):
    for f in instance._meta.get_fields():
        gf = getattr(f, "target_field", f)
        if isinstance(gf, GeometryField):
            name = getattr(f, "attname", f.name)
            val = getattr(instance, name, None)
            if val is not None:
                setattr(instance, name, _geos_force2d(val))

# ---------------- Lógica simples de dono ----------------


def _resolve_dono(user):
    # Mesmo padrão que você já tinha: se for dono, ele próprio; senão, FK user.dono
    if getattr(user, "role", None) == "dono":
        return user
    dono = getattr(user, "dono", None)
    # opcional: valida papel
    if dono is not None and getattr(dono, "role", None) == "dono":
        return dono
    return None

# ------------------------------------------------------------------------------
# LISTA/EDITA/EXCLUI
# ------------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_projects(request):
    user = request.user

    # se for dono, ele é o tenant
    # se for adm/comum, o tenant é o dono dele
    if getattr(user, "role", None) == "dono":
        tenant = user
    else:
        tenant = user.dono or user  # fallback se por acaso dono vier vazio

    qs = Project.objects.filter(dono=tenant).order_by(
        "-updated_at", "-created_at")

    # se quiser que superuser veja tudo:
    # if user.is_superuser:
    #     qs = Project.objects.all().order_by("-updated_at", "-created_at")

    serializer = ProjectSerializer(qs, many=True)
    return Response(serializer.data)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def update_delete_project(request, pk: int):
    proj = get_object_or_404(Project, pk=pk)

    # GET: qualquer usuário autenticado pode VER (igual você já faz em outros endpoints)
    if request.method == "GET":
        ser = ProjectSerializer(proj)
        return Response(ser.data)

    # PATCH/DELETE: precisa ter permissão de edição
    if not _same_tenant_or_owner(request.user, proj):
        return Response({"detail": "Sem permissão."}, status=403)

    if request.method == "DELETE":
        proj.delete()
        return Response(status=204)

    # PATCH
    ser = ProjectSerializer(proj, data=request.data, partial=True)
    if ser.is_valid():
        ser.save()
        return Response(ser.data)
    return Response(ser.errors, status=400)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def project_map_summary(request, pk: int):
    proj = get_object_or_404(Project, pk=pk)

    counts, colors = {}, {}
    for pf in ProjectFeature.objects.filter(project=proj).only("overlay_id", "color").iterator():
        counts[pf.overlay_id] = counts.get(pf.overlay_id, 0) + 1
        if pf.color and pf.overlay_id not in colors:
            colors[pf.overlay_id] = pf.color

    overlays = [
        {"overlay_id": k, "count": counts.get(k, 0), "color": colors.get(k)}
        for k in sorted(counts.keys())
    ]

    return Response({
        "id": proj.id,
        "name": proj.name,
        "description": proj.description,
        "uf": proj.uf,
        "municipio": proj.municipio,
        "aoi": json.loads(proj.aoi_geom.geojson) if proj.aoi_geom else None,
        "layer_flags": proj.layer_flags or {},
        "overlays": overlays,
        "dono": proj.dono_id,
        "owner": proj.owner_id,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def project_features_geojson(request, pk: int):
    # ✅ qualquer usuário autenticado pode visualizar
    proj = get_object_or_404(Project, pk=pk)

    overlay_id = request.query_params.get("overlay_id")
    if not overlay_id:
        return Response({"detail": "overlay_id é obrigatório."}, status=400)

    simplified = str(request.query_params.get("simplified", "true")).lower() in {
        "1", "true", "yes", "y"}

    feats = []
    qs = ProjectFeature.objects.filter(project=proj, overlay_id=overlay_id).only(
        "geom", "geom_simpl", "properties", "color"
    )
    for pf in qs.iterator():
        g = pf.geom_simpl if simplified and pf.geom_simpl else pf.geom
        if not g:
            continue
        feats.append({
            "type": "Feature",
            "properties": {**(pf.properties or {}), "__overlay_id": overlay_id, "__color": pf.color},
            "geometry": json.loads(g.geojson),
        })

    return Response({"type": "FeatureCollection", "features": feats})


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def project_overlay_patch(request, pk: int):
    proj = get_object_or_404(Project, pk=pk)
    if not _same_tenant_or_owner(request.user, proj):
        return Response({"detail": "Sem permissão."}, status=403)
    overlay_id = (request.data or {}).get("overlay_id")
    if not overlay_id:
        return Response({"detail": "overlay_id é obrigatório."}, status=400)
    new_overlay_id = (request.data or {}).get("new_overlay_id")
    color = (request.data or {}).get("color")
    qs = ProjectFeature.objects.filter(project=proj, overlay_id=overlay_id)
    if new_overlay_id:
        qs.update(overlay_id=str(new_overlay_id)[:200])
    if color is not None:
        qs.update(color=str(color)[:16])
    return Response({"ok": True})


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def project_overlay_delete(request, pk: int):
    proj = get_object_or_404(Project, pk=pk)
    if not _same_tenant_or_owner(request.user, proj):
        return Response({"detail": "Sem permissão."}, status=403)
    overlay_id = request.query_params.get("overlay_id")
    if not overlay_id:
        return Response({"detail": "overlay_id é obrigatório."}, status=400)
    deleted, _ = ProjectFeature.objects.filter(
        project=proj, overlay_id=overlay_id).delete()
    return Response({"deleted": deleted})


# ------------------------------------------------------------------------------
# EXPORTAR + UPSERT
# ------------------------------------------------------------------------------

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def exportar_projeto(request):
    """
    Exporta KML/KMZ com base na AOI + camadas.

    Comportamento controlado por 'persist':
    - persist=True  -> salva/atualiza Project, ProjectFeature, camadas base, artifact, snapshot.
    - persist=False -> NÃO salva nada no banco (usa transação com rollback), só gera o arquivo.
    """
    s = ProjectUpsertExportSerializer(data=request.data or {})
    if not s.is_valid():
        return Response(s.errors, status=status.HTTP_400_BAD_REQUEST)
    v = s.validated_data

    user = request.user

    # -------- Flags principais --------
    persist = v.get("persist", True)  # <- controla salvar x só exportar

    # -------- Entrada principal --------
    aoi: MultiPolygon = v["aoi"]
    simplify = v.get("simplify") or {}
    out_format = (v.get("format") or "kmz").lower()
    replace_overlays = bool(v.get("replace_overlays", False))

    project_id = v.get("project_id")
    name = (v.get("project_name") or v.get("name") or "").strip() or "Projeto"
    description = v.get("project_description", v.get("description", "")) or ""
    uf = v.get("uf") or None
    municipio = (v.get("municipio") or "").strip() or None

    # Aceita "layers" (novo) ou "layer_flags" (legado)
    layers = (v.get("layers") or v.get("layer_flags") or {}) or {}

    # Dono/owner
    dono_user = user if getattr(user, "role", None) == "dono" else getattr(
        user, "dono", None
    ) or user
    created = False

    # ---------- Helpers locais ----------
    def _force2d_sql(expr):
        """Wrap em ST_Force2D(expr)"""
        return Func(expr, function="ST_Force2D", output_field=GeometryField(srid=4326))

    def _db_intersection_2d(geom: GEOSGeometry, aoi_mp: GEOSGeometry) -> Optional[GEOSGeometry]:
        """Interseção no PostGIS forçando 2D em entradas e saída."""
        if not geom or geom.empty or not aoi_mp or aoi_mp.empty:
            return None
        try:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT ST_AsEWKB(
                        ST_Force2D(
                            ST_Intersection(
                                ST_MakeValid(ST_Force2D(%s::geometry)),
                                ST_MakeValid(ST_Force2D(%s::geometry))
                            )
                        )
                    )
                    """,
                    [geom.ewkb, aoi_mp.ewkb],
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    return None
                return GEOSGeometry(memoryview(row[0]))
        except Exception:
            return None

    def _iter_features(fc: Dict):
        if not fc:
            return []
        t = fc.get("type")
        if t == "FeatureCollection":
            return fc.get("features", []) or []
        if t == "Feature":
            return [fc]
        return []

    def _geos_from_json_2d(geom_obj: Any) -> GEOSGeometry:
        """GEOS em 4326 e 2D (remove Z implicitamente)."""
        g = GEOSGeometry(json.dumps(geom_obj) if isinstance(
            geom_obj, (dict, list)) else str(geom_obj)
        )
        if g.srid in (None, 0):
            g.srid = 4326
        elif g.srid != 4326:
            try:
                g.transform(4326)
            except Exception:
                pass
        # “achata” para 2D se veio com Z
        try:
            if getattr(g, "hasz", False):
                from django.contrib.gis.geos import WKBWriter
                w = WKBWriter()
                w.outdim = 2
                g = GEOSGeometry(w.write(g), srid=g.srid or 4326)
        except Exception:
            g = GEOSGeometry(g.wkt, srid=g.srid or 4326)
        if not g.valid:
            try:
                g = g.buffer(0)
            except Exception:
                pass
        return g

    # ---------- Variáveis que serão usadas fora da transação ----------
    km_bytes = None
    filename = None
    content_type = None
    overlays_used: Set[str] = set()
    feats = []
    total_clip = 0

    # ---------- UPSERT projeto + features + KML/KMZ ----------
    with transaction.atomic():
        # --- UPSERT Project ---
        if project_id:
            proj = get_object_or_404(
                Project.objects.select_for_update(), pk=project_id
            )
            if not _same_tenant_or_owner(user, proj):
                return Response({"detail": "Sem permissão."}, status=403)
            # Atualiza metadados apenas se estiver persistindo
            if persist:
                proj.name = name
                proj.description = description
                proj.uf = uf
                proj.municipio = municipio
        else:
            found = Project.objects.select_for_update().filter(
                dono=dono_user, name=name
            ).first()
            if found:
                proj = found
                if persist:
                    proj.description = description
                    proj.uf = uf
                    proj.municipio = municipio
            else:
                # Mesmo com persist=False, criamos um Project “temporário” só
                # para servir de contexto; o rollback depois apaga.
                proj = Project(
                    name=name,
                    description=description,
                    municipio=municipio,
                    uf=uf,
                    owner=user,
                    dono=dono_user,
                )
                proj.save()
                created = True

        # AOI 2D (sempre, para o KMZ)
        proj.aoi_geom = _geos_from_json_2d(json.loads(aoi.geojson))

        # layer_flags ficam apenas em memória se persist=False;
        # se persist=True, acabam salvos em banco.
        if layers:
            proj.layer_flags = layers

        if persist:
            proj.save()

        # ---------- Overlays do payload ----------
        src_fc = v.get("overlays_raw") or v.get("overlays") or {
            "type": "FeatureCollection",
            "features": [],
        }
        feats = list(_iter_features(src_fc))

        tol_lines = float(
            simplify.get("lines", simplify.get(
                "lt", simplify.get("rios", 0.00002)))
            or 0.00002
        )
        if "polygons" in simplify:
            tol_polys = float(simplify["polygons"])  # pode ser 0
        else:
            tol_polys = float(simplify.get("polygon", 0.00005))

        to_create = []
        overlays_used = set()
        overlays_touched: Set[str] = set()
        total_in = 0
        total_clip = 0

        for f in feats:
            geom = f.get("geometry")
            if not geom:
                continue
            total_in += 1
            props = (f.get("properties") or {}).copy()
            overlay_id = props.pop(
                "__overlay_id",
                props.get("overlay_id") or props.get("name") or "overlay",
            )
            color = props.pop("__color", None)

            g = _geos_from_json_2d(geom)
            inter = _db_intersection_2d(g, proj.aoi_geom)
            if not inter or inter.empty:
                continue
            total_clip += 1

            try:
                if inter.geom_type in (
                    "LineString",
                    "MultiLineString",
                    "GeometryCollection",
                ):
                    g_simpl = inter.simplify(tol_lines, preserve_topology=True)
                else:
                    g_simpl = inter.simplify(tol_polys, preserve_topology=True)
            except Exception:
                g_simpl = inter

            to_create.append(
                ProjectFeature(
                    project=proj,
                    overlay_id=str(overlay_id)[:200],
                    properties=props,
                    color=(str(color)[:16] if color else None),
                    geom=inter,
                    geom_simpl=g_simpl,
                    created_by=user,
                )
            )
            overlays_used.add(str(overlay_id))
            overlays_touched.add(str(overlay_id))

        if persist:
            if replace_overlays and overlays_touched:
                ProjectFeature.objects.filter(
                    project=proj, overlay_id__in=list(overlays_touched)
                ).delete()
            if to_create:
                ProjectFeature.objects.bulk_create(to_create, batch_size=1000)

        # ---------- Camadas base (rios, LT, etc.) ----------
        base_creates = []

        def _save_lines(Model, overlay_name: str):
            ids = (
                Model.objects.filter(geom__intersects=proj.aoi_geom)
                .order_by("id")
                .values_list("id", flat=True)
            )
            for batch in _yield_ids_in_batches(ids, batch_size=2000):
                qs = (
                    Model.objects.filter(id__in=batch)
                    .annotate(
                        clipped=Intersection(
                            "geom",
                            Value(
                                proj.aoi_geom,
                                output_field=GeometryField(srid=4326),
                            ),
                        )
                    )
                )
                # Força 2D ANTES de MakeValid/Simplify
                qs = qs.annotate(clipped2d=_force2d_sql(F("clipped")))
                qs = qs.annotate(geom_valid=MakeValid(F("clipped2d"))).annotate(
                    geom_simpl=Func(
                        F("geom_valid"),
                        Value(float(tol_lines)),
                        function="ST_SimplifyPreserveTopology",
                        output_field=GeometryField(srid=4326),
                    )
                )
                for row in qs.only("id"):
                    g = getattr(row, "geom_simpl", None) or getattr(
                        row, "geom_valid", None
                    )
                    if g and not g.empty:
                        base_creates.append(
                            ProjectFeature(
                                project=proj,
                                overlay_id=overlay_name,
                                properties={},
                                color=None,
                                geom=g,
                                geom_simpl=g,
                                created_by=user,
                            )
                        )

        def _save_polys(Model, overlay_name: str, extra_filter=None):
            base = Model.objects.filter(geom__intersects=proj.aoi_geom)
            if extra_filter:
                base = base.filter(**extra_filter)
            ids = base.order_by("id").values_list("id", flat=True)
            for batch in _yield_ids_in_batches(ids, batch_size=1000):
                qs = (
                    Model.objects.filter(id__in=batch)
                    .annotate(
                        clipped=Intersection(
                            "geom",
                            Value(
                                proj.aoi_geom,
                                output_field=GeometryField(srid=4326),
                            ),
                        )
                    )
                )
                # Força 2D ANTES de MakeValid/Simplify
                qs = qs.annotate(clipped2d=_force2d_sql(F("clipped")))
                qs = qs.annotate(geom_valid=MakeValid(F("clipped2d"))).annotate(
                    geom_simpl=Func(
                        F("geom_valid"),
                        Value(float(tol_polys)),
                        function="ST_SimplifyPreserveTopology",
                        output_field=GeometryField(srid=4326),
                    )
                )
                for row in qs.only("id"):
                    g = getattr(row, "geom_simpl", None) or getattr(
                        row, "geom_valid", None
                    )
                    if g and not g.empty:
                        base_creates.append(
                            ProjectFeature(
                                project=proj,
                                overlay_id=overlay_name,
                                properties={},
                                color=None,
                                geom=g,
                                geom_simpl=g,
                                created_by=user,
                            )
                        )

        if persist:
            if layers.get("rios"):
                _save_lines(Waterway, "Rios")
            if layers.get("lt"):
                _save_lines(LinhaTransmissao, "Linhas de Transmissão")
            if layers.get("mf"):
                _save_lines(MalhaFerroviaria, "Ferrovias")
            if layers.get("cidades"):
                _save_polys(Cidade, "Municípios")
            if layers.get("limites_federais"):
                _save_polys(LimiteFederal, "Áreas Federais")
            if layers.get("areas_estaduais"):
                extra = {"uf": proj.uf} if proj.uf else None
                _save_polys(Area, "Áreas Estaduais", extra_filter=extra)

            if base_creates:
                ProjectFeature.objects.bulk_create(
                    base_creates, batch_size=1000)

        # ---------- Gera KML/KMZ ainda DENTRO da transação ----------
        km_bytes, filename, content_type = build_kmz_from_payload(
            project=proj,
            aoi_geojson=json.loads(proj.aoi_geom.geojson),
            layer_flags=proj.layer_flags or {},
            simplify=simplify,
            include_saved_overlays=True,
            out_format=out_format,
        )

        # Se NÃO for pra persistir, marcamos rollback:
        # nada do que fizemos no banco é realmente gravado.
        if not persist:
            transaction.set_rollback(True)

    # ---------- Artefato + snapshot (só se persist=True) ----------
    if persist:
        try:
            artifact = MapArtifact.objects.create(
                project=proj,
                kind="export",
                content_type=content_type,
                size_bytes=len(km_bytes),
                meta={"filename": filename},
            )
            artifact.file.save(filename, ContentFile(km_bytes), save=True)
            ExportSnapshot.objects.create(
                project=proj,
                artifact=artifact,
                aoi_geom=proj.aoi_geom,
                layer_flags=proj.layer_flags or {},
                overlays_used=sorted(overlays_used),
                created_by=user,
            )
        except Exception:
            pass

    # ---------- Resposta ----------
    resp = HttpResponse(km_bytes, content_type=content_type)
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'

    if persist:
        resp["X-Proj-Id"] = str(proj.id)
        resp["X-Proj-Created"] = "1" if created else "0"
    else:
        # Exportação “volátil”: não garante que o projeto exista após a request
        resp["X-Proj-Id"] = ""
        resp["X-Proj-Created"] = "0"

    resp["X-Overlays-Used"] = ",".join(sorted(overlays_used))
    resp["X-Features-In"] = str(len(feats))
    resp["X-Features-Clipped"] = str(total_clip)
    return resp
