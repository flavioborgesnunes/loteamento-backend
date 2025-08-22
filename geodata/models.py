from django.contrib.gis.db import models


class Cidade(models.Model):
    name = models.CharField(max_length=255, null=True,
                            blank=True, db_index=True)
    source = models.CharField(max_length=128, null=True, blank=True)
    geom = models.MultiPolygonField(srid=4326, spatial_index=True)


class LinhaTransmissao(models.Model):
    name = models.CharField(max_length=255, null=True,
                            blank=True, db_index=True)
    source = models.CharField(max_length=128, null=True, blank=True)
    geom = models.MultiLineStringField(srid=4326, spatial_index=True)


class MalhaFerroviaria(models.Model):
    name = models.CharField(max_length=255, null=True,
                            blank=True, db_index=True)
    source = models.CharField(max_length=128, null=True, blank=True)
    geom = models.MultiLineStringField(srid=4326, spatial_index=True)

    class Meta:
        db_table = 'geodata_malha_ferroviaria'
        managed = False


class LimiteFederal(models.Model):
    name = models.CharField(max_length=255, null=True,
                            blank=True, db_index=True)
    source = models.CharField(max_length=128, null=True, blank=True)
    geom = models.MultiPolygonField(srid=4326, spatial_index=True)


class Area(models.Model):
    uf = models.CharField(max_length=2, null=True, blank=True, db_index=True)
    name = models.CharField(max_length=255, null=True,
                            blank=True, db_index=True)
    source = models.CharField(max_length=128, null=True, blank=True)
    geom = models.MultiPolygonField(srid=4326, spatial_index=True)
