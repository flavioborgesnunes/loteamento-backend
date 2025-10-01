from __future__ import annotations
from django.conf import settings
from django.contrib.gis.db import models
from django.contrib.postgres.indexes import GistIndex

SRID_WGS = 4674  # SIRGAS2000

class Restricoes(models.Model):
    project = models.ForeignKey("projetos.Project", on_delete=models.CASCADE, related_name="restricoes_versions")
    version = models.PositiveIntegerField(editable=False)
    aoi_snapshot = models.MultiPolygonField(srid=SRID_WGS, null=True, blank=True)

    label = models.CharField(max_length=120, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    percent_permitido = models.FloatField(null=True, blank=True)
    corte_pct_cache = models.FloatField(null=True, blank=True)

    source = models.CharField(max_length=40, default="geoman")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        unique_together = (("project", "version"),)
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.pk and not self.version:
            last = (Restricoes.objects.filter(project=self.project).order_by("-version").values_list("version", flat=True).first())
            self.version = (last or 0) + 1
        return super().save(*args, **kwargs)

class AreaVerdeV(models.Model):
    restricoes = models.ForeignKey(Restricoes, on_delete=models.CASCADE, related_name="areas_verdes")
    geom = models.MultiPolygonField(srid=SRID_WGS)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        indexes = [models.Index(fields=["restricoes"]), GistIndex(fields=["geom"])]

class CorteAreaVerdeV(models.Model):
    restricoes = models.ForeignKey(Restricoes, on_delete=models.CASCADE, related_name="cortes_av")
    geom = models.MultiPolygonField(srid=SRID_WGS)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        indexes = [models.Index(fields=["restricoes"]), GistIndex(fields=["geom"])]

class RuaV(models.Model):
    restricoes = models.ForeignKey(Restricoes, on_delete=models.CASCADE, related_name="ruas")
    eixo = models.MultiLineStringField(srid=SRID_WGS)             # o que o front desenha
    largura_m = models.FloatField(default=12.0)                   # o que o front informa
    mask = models.MultiPolygonField(srid=SRID_WGS, null=True, blank=True)  # “máscara” persistida (buffer largura/2)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        indexes = [models.Index(fields=["restricoes"]), GistIndex(fields=["eixo"]), GistIndex(fields=["mask"])]

class MargemRioV(models.Model):
    restricoes = models.ForeignKey(Restricoes, on_delete=models.CASCADE, related_name="margens_rio")
    centerline = models.MultiLineStringField(srid=SRID_WGS)
    margem_m = models.FloatField(default=30.0)
    faixa = models.MultiPolygonField(srid=SRID_WGS, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        indexes = [models.Index(fields=["restricoes"]), GistIndex(fields=["centerline"]), GistIndex(fields=["faixa"])]

class MargemLTV(models.Model):
    restricoes = models.ForeignKey(Restricoes, on_delete=models.CASCADE, related_name="margens_lt")
    centerline = models.MultiLineStringField(srid=SRID_WGS)
    margem_m = models.FloatField(default=15.0)
    faixa = models.MultiPolygonField(srid=SRID_WGS, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        indexes = [models.Index(fields=["restricoes"]), GistIndex(fields=["centerline"]), GistIndex(fields=["faixa"])]

class MargemFerroviaV(models.Model):
    restricoes = models.ForeignKey(Restricoes, on_delete=models.CASCADE, related_name="margens_ferrovia")
    centerline = models.MultiLineStringField(srid=SRID_WGS)
    margem_m = models.FloatField(default=20.0)
    faixa = models.MultiPolygonField(srid=SRID_WGS, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        indexes = [models.Index(fields=["restricoes"]), GistIndex(fields=["centerline"]), GistIndex(fields=["faixa"])]
