from django.db import migrations, models
import django.contrib.gis.db.models.fields
import django.db.models.deletion
from django.conf import settings

class Migration(migrations.Migration):

    initial = True
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("projetos","0001_initial"),  # ajuste para sua app de projetos
    ]

    operations = [
        migrations.CreateModel(
            name='Restricoes',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False, auto_created=True, verbose_name='ID')),
                ('version', models.PositiveIntegerField(editable=False)),
                ('aoi_snapshot', django.contrib.gis.db.models.fields.MultiPolygonField(srid=4674, null=True, blank=True)),
                ('label', models.CharField(max_length=120, blank=True, default='')),
                ('notes', models.TextField(blank=True, default='')),
                ('percent_permitido', models.FloatField(null=True, blank=True)),
                ('corte_pct_cache', models.FloatField(null=True, blank=True)),
                ('source', models.CharField(max_length=40, default='geoman')),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),

                # ⬇⬇⬇ AQUI: use AUTH_USER_MODEL, não "auth.user"
                ('created_by', models.ForeignKey(
                    null=True, blank=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to=settings.AUTH_USER_MODEL,
                )),

                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='restricoes_versions',
                    to='projetos.project'
                )),
            ],
            options={'ordering': ['-created_at'], 'unique_together': {('project', 'version')}},
        ),

        migrations.CreateModel(
            name="AreaVerdeV",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False, auto_created=True, verbose_name="ID")),
                ("geom", django.contrib.gis.db.models.fields.MultiPolygonField(srid=4674)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("restricoes", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="areas_verdes", to="restricoes.restricoes")),
            ],
        ),
        migrations.CreateModel(
            name="CorteAreaVerdeV",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False, auto_created=True, verbose_name="ID")),
                ("geom", django.contrib.gis.db.models.fields.MultiPolygonField(srid=4674)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("restricoes", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="cortes_av", to="restricoes.restricoes")),
            ],
        ),
        migrations.CreateModel(
            name="RuaV",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False, auto_created=True, verbose_name="ID")),
                ("eixo", django.contrib.gis.db.models.fields.MultiLineStringField(srid=4674)),
                ("largura_m", models.FloatField(default=12.0)),
                ("mask", django.contrib.gis.db.models.fields.MultiPolygonField(srid=4674, null=True, blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("restricoes", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ruas", to="restricoes.restricoes")),
            ],
        ),
        migrations.CreateModel(
            name="MargemRioV",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False, auto_created=True, verbose_name="ID")),
                ("centerline", django.contrib.gis.db.models.fields.MultiLineStringField(srid=4674)),
                ("margem_m", models.FloatField(default=30.0)),
                ("faixa", django.contrib.gis.db.models.fields.MultiPolygonField(srid=4674, null=True, blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("restricoes", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="margens_rio", to="restricoes.restricoes")),
            ],
        ),
        migrations.CreateModel(
            name="MargemLTV",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False, auto_created=True, verbose_name="ID")),
                ("centerline", django.contrib.gis.db.models.fields.MultiLineStringField(srid=4674)),
                ("margem_m", models.FloatField(default=15.0)),
                ("faixa", django.contrib.gis.db.models.fields.MultiPolygonField(srid=4674, null=True, blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("restricoes", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="margens_lt", to="restricoes.restricoes")),
            ],
        ),
        migrations.CreateModel(
            name="MargemFerroviaV",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False, auto_created=True, verbose_name="ID")),
                ("centerline", django.contrib.gis.db.models.fields.MultiLineStringField(srid=4674)),
                ("margem_m", models.FloatField(default=20.0)),
                ("faixa", django.contrib.gis.db.models.fields.MultiPolygonField(srid=4674, null=True, blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("restricoes", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="margens_ferrovia", to="restricoes.restricoes")),
            ],
        ),
    ]
