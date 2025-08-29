from django.conf import settings
from django.contrib.gis.db import models as gis
from django.contrib.postgres.indexes import GistIndex
from django.db import models

User = settings.AUTH_USER_MODEL


class Project(models.Model):
    """
    Projeto criado por um usuário. Todo projeto pertence a um DONO (tenant)
    e tem um OWNER (quem criou). Um projeto guarda a AOI e flags de camadas.
    """
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, null=True)

    dono = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="projetos_dono",
        limit_choices_to={'role': 'dono'}
    )
    owner = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="projetos_owner"
    )

    uf = models.CharField(max_length=2, blank=True, null=True)
    aoi_geom = gis.MultiPolygonField(srid=4326, null=True, blank=True)
    layer_flags = models.JSONField(
        default=dict, blank=True)  # ex.: {"rios":true, ...}

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.id})"


class MapArtifact(models.Model):
    """
    Arquivos (KML/KMZ) ligados ao projeto (export final, etc.)
    """
    KIND_CHOICES = [
        ("export", "Export final"),
        ("overlay_src", "Overlay fonte"),
        ("aoi_src", "AOI fonte"),
        ("outro", "Outro"),
    ]
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="artifacts")
    kind = models.CharField(
        max_length=20, choices=KIND_CHOICES, default="export")
    file = models.FileField(upload_to="projetos/%Y/%m/%d/")
    content_type = models.CharField(max_length=100, blank=True, null=True)
    size_bytes = models.BigIntegerField(default=0)
    # ex.: filename, bbox, contagens
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ProjectFeature(models.Model):
    """
    Features (já recortadas pela AOI) vindas dos KMLs secundários (ou outras fontes).
    Guarda geom normal e opcionalmente uma versão simplificada.
    """
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="features")
    # vem do __overlay_id (ex.: "Zoneamento", "APP", ...)
    overlay_id = models.CharField(max_length=200)
    properties = models.JSONField(default=dict, blank=True)
    color = models.CharField(max_length=16, blank=True, null=True)

    # LineString/MultiLineString/Polygon/MultiPolygon
    geom = gis.GeometryField(srid=4326)
    geom_simpl = gis.GeometryField(srid=4326, null=True, blank=True)

    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="features_criadas")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            GistIndex(fields=["geom"]),
            GistIndex(fields=["geom_simpl"]),
            models.Index(fields=["overlay_id"]),
        ]


class ExportSnapshot(models.Model):
    """
    Registro de cada export: conserva parâmetros utilizados e link para o arquivo gerado.
    """
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="exports")
    artifact = models.ForeignKey(
        MapArtifact, on_delete=models.SET_NULL, null=True, blank=True, related_name="snapshot")

    aoi_geom = gis.MultiPolygonField(srid=4326)
    layer_flags = models.JSONField(default=dict)
    # lista de overlay_id incluídos
    overlays_used = models.JSONField(default=list)

    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="exports_criados")
    created_at = models.DateTimeField(auto_now_add=True)
