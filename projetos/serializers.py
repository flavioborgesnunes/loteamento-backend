from __future__ import annotations

import json

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon
from rest_framework import serializers

from .models import Project


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ["id", "name", "description", "uf", "dono",
                  "owner", "layer_flags", "created_at", "updated_at"]


class ProjectUpsertExportSerializer(serializers.Serializer):
    """
    Export + persistência de overlays:
      - Se 'project_id' vier, usa o projeto existente.
      - Caso contrário, faz UPSERT por (dono, project_name):
          * se existir, atualiza;
          * se não, cria.
    """
    # Identificação do projeto
    project_id = serializers.IntegerField(required=False, allow_null=True)
    project_name = serializers.CharField(
        required=False, allow_blank=True, default="")
    project_description = serializers.CharField(
        required=False, allow_blank=True, default="")
    uf = serializers.CharField(required=False, allow_blank=True, default="")

    # Dados do mapa
    aoi = serializers.JSONField()
    layers = serializers.JSONField(required=False, default=dict)
    simplify = serializers.JSONField(required=False, default=dict)
    overlays_raw = serializers.JSONField(required=False, default=dict)
    overlays = serializers.JSONField(required=False, default=dict)

    # Opções
    format = serializers.ChoiceField(
        choices=["kml", "kmz"], required=False, default="kmz")
    replace_overlays = serializers.BooleanField(required=False, default=False)

    # --- validações geométricas ---
    def validate_aoi(self, value):
        # Aceita dict GeoJSON ou string; devolve GEOS MP (SRID 4326)
        try:
            g = GEOSGeometry(json.dumps(value)) if isinstance(
                value, dict) else GEOSGeometry(str(value))
        except Exception:
            raise serializers.ValidationError("AOI inválida.")
        if g.srid in (None, 0):
            g.srid = 4326
        elif g.srid != 4326:
            try:
                g.transform(4326)
            except Exception:
                pass
        if not g.valid:
            try:
                g = g.buffer(0)
            except Exception:
                pass
        if g.geom_type == "Polygon":
            g = MultiPolygon([g], srid=4326)
        if g.geom_type != "MultiPolygon":
            raise serializers.ValidationError(
                "AOI deve ser Polygon/MultiPolygon.")
        return g

    def validate_uf(self, v):
        v = (v or "").upper().strip()
        return v[:2] or ""

    def validate(self, attrs):
        # Se não vier project_id, exigimos um project_name para o UPSERT
        if not attrs.get("project_id") and not (attrs.get("project_name") or "").strip():
            raise serializers.ValidationError(
                {"project_name": "Obrigatório quando 'project_id' não for enviado."})
        return attrs
