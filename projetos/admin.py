from django.contrib import admin

from .models import ExportSnapshot, MapArtifact, Project, ProjectFeature


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "dono", "owner", "uf", "created_at")
    search_fields = ("name", "owner__email", "dono__email")
    list_filter = ("uf",)


@admin.register(MapArtifact)
class MapArtifactAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "kind", "content_type",
                    "size_bytes", "created_at")


@admin.register(ProjectFeature)
class ProjectFeatureAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "overlay_id", "created_at")
    search_fields = ("overlay_id",)


@admin.register(ExportSnapshot)
class ExportSnapshotAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "artifact", "created_at")


class ExportSnapshotAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "artifact", "created_at")
