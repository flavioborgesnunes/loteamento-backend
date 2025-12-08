from django.urls import include, path
from geodata import views as geodata_views
from ia import views as ia_views
from iaparcelamento import views as ia_parcelamento_views
from parcelamento.views import PlanoViewSet, VersaoViewSet
# Projetos (funções avulsas)
from projetos.views import (exportar_projeto, list_projects,
                            project_features_geojson, project_map_summary,
                            project_overlay_delete, project_overlay_patch,
                            update_delete_project)
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView
# ViewSets do app restricoes
from restricoes import views as restricoes_views
from rios import views as rios_views
# Apps existentes
from userauth import views as userauths_views

# Parcelamento: mapeia métodos HTTP → ações do ViewSet
plano_list = PlanoViewSet.as_view({"get": "list", "post": "create"})
plano_detail = PlanoViewSet.as_view(
    {"get": "retrieve", "patch": "partial_update", "put": "update", "delete": "destroy"})
plano_preview = PlanoViewSet.as_view({"post": "preview"})
plano_material = PlanoViewSet.as_view({"post": "materializar"})
plano_recalcular = PlanoViewSet.as_view({"post": "recalcular"})  # NOVO

versao_list = VersaoViewSet.as_view({"get": "list"})
versao_detail = VersaoViewSet.as_view({"get": "retrieve"})
versao_geojson = VersaoViewSet.as_view({"get": "geojson"})
versao_kml = VersaoViewSet.as_view({"post": "kml"})
versao_geojson_bord = VersaoViewSet.as_view(
    {"get": "geojson_com_bordas"})  # NOVO


# Parcelamento: mapeia métodos HTTP → ações do ViewSet
plano_list = PlanoViewSet.as_view({"get": "list", "post": "create"})
plano_detail = PlanoViewSet.as_view(
    {"get": "retrieve", "patch": "partial_update", "put": "update", "delete": "destroy"})
plano_preview = PlanoViewSet.as_view({"post": "preview"})
plano_material = PlanoViewSet.as_view({"post": "materializar"})

versao_list = VersaoViewSet.as_view({"get": "list"})
versao_detail = VersaoViewSet.as_view({"get": "retrieve"})
versao_geojson = VersaoViewSet.as_view({"get": "geojson"})
versao_kml = VersaoViewSet.as_view({"post": "kml"})

# ------------------------------------------------------------------------------
# URL patterns
# ------------------------------------------------------------------------------
urlpatterns = [
    # User / Auth
    path("user/", userauths_views.UserView.as_view()),
    path("user/update/", userauths_views.UserUpdateView.as_view()),
    path("user/token/", userauths_views.MyTokenObtainPairView.as_view()),
    path("user/token/refresh/", TokenRefreshView.as_view()),
    path("user/register/", userauths_views.RegisterView.as_view()),
    path("user/password-reset/<email>/",
         userauths_views.PasswordResetEmailVerify.as_view()),
    path("user/password-change/", userauths_views.PasswordChangeView.as_view()),
    path("user/usuarios-do-dono/", userauths_views.UsuariosDoDonoListView.as_view()),


    # IA
    path("autofill/", ia_views.LegalAutoFillView.as_view(), name="autofill"),

    # Rios
    path("rios/geojson/", rios_views.rios_geojson, name="rios-geojson"),
    path("export/rios/", rios_views.export_rios_kmz, name="export-rios-kmz"),

    # Geodata (legado/compatibilidade)
    path("export/mapa/", geodata_views.export_mapa_kmz, name="export-mapa-kmz"),

    # Projetos
    path("projetos/", list_projects),  # GET /projetos/
    path("projetos/<int:pk>/", update_delete_project),  # PATCH/DELETE
    path("projetos/<int:pk>/map/summary/", project_map_summary),
    path("projetos/<int:pk>/features/", project_features_geojson),
    path("projetos/<int:pk>/overlay/", project_overlay_patch),
    path("projetos/<int:pk>/overlay/delete/", project_overlay_delete),
    path("projetos/exportar/", exportar_projeto),

    # Restrições
    path("projetos/<int:project_id>/restricoes/",
         restricoes_views.RestricoesCreateAPIView.as_view()),
    path("projetos/<int:project_id>/restricoes/list/",
         restricoes_views.RestricoesListByProjectAPIView.as_view()),
    path("restricoes/<int:restricoes_id>/geo/",
         restricoes_views.RestricoesGeoDetailAPIView.as_view()),
    path(
        "restricoes/todas-do-dono/",
        restricoes_views.RestricoesListByDonoAPIView.as_view(),
    ),
    path(
        "restricoes/<int:restricoes_id>/exportar-kmz/",
        restricoes_views.RestricoesExportKmzAPIView.as_view(),
        name="restricoes-exportar-kmz",
    ),

    path(
        "restricoes/<int:restricoes_id>/",
        restricoes_views.RestricoesUpdateAPIView.as_view(),
    ),


    # Parcelamento:
    # Planos
    path("parcelamento/planos/", plano_list, name="parcelamento-planos-list"),
    path("parcelamento/planos/<int:pk>/", plano_detail,
         name="parcelamento-planos-detail"),
    path("parcelamento/planos/<int:pk>/preview/",
         plano_preview, name="parcelamento-planos-preview"),
    path("parcelamento/planos/<int:pk>/materializar/",
         plano_material, name="parcelamento-planos-materializar"),
    path("parcelamento/planos/<int:pk>/recalcular/", plano_recalcular,
         name="parcelamento-planos-recalcular"),

    # Versões
    path("parcelamento/versoes/", versao_list,
         name="parcelamento-versoes-list"),
    path("parcelamento/versoes/<int:pk>/", versao_detail,
         name="parcelamento-versoes-detail"),
    path("parcelamento/versoes/<int:pk>/geojson/",
         versao_geojson, name="parcelamento-versoes-geojson"),
    path("parcelamento/versoes/<int:pk>/geojson-bordas/", versao_geojson_bord,
         name="parcelamento-versoes-geojson-bordas"),
    path("parcelamento/versoes/<int:pk>/kml/",
         versao_kml, name="parcelamento-versoes-kml"),

    # IA Parcelamento
    path(
        "ia-parcelamento/planos/<int:plano_id>/sugerir-parametros/",
        ia_parcelamento_views.SugerirParametrosView.as_view(),
        name="ia-parcelamento-sugerir-parametros",
    ),
    path(
        "ia-parcelamento/planos/<int:plano_id>/preview/",
        ia_parcelamento_views.PreviewIaView.as_view(),
        name="ia-parcelamento-preview",
    ),
    path(
        "ia-parcelamento/planos/<int:plano_id>/svg-preview/",
        ia_parcelamento_views.SvgPreviewIaView.as_view(),
        name="ia-parcelamento-svg-preview",
    ),


]
