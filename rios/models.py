# rios/models.py
from django.contrib.gis.db import models


class Waterway(models.Model):
    # geometria única para todo o Brasil (WGS84)
    geom = models.MultiLineStringField(srid=4326, spatial_index=True)
    # campos opcionais (ajuste conforme seu geojson)
    name = models.CharField(max_length=255, null=True, blank=True)
    source = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["name"]),
        ]
        verbose_name = "Rio"
        verbose_name_plural = "Rios"

    def __str__(self):
        return self.name or f"Rio {self.pk}"
