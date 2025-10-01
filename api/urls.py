from django.urls import include, path
from geodata import views as geodata_views
from ia import views as ia_views
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

# ------------------------------------------------------------------------------
# DRF Router para os endpoints de "restricoes"
# ------------------------------------------------------------------------------

# restricoes_router = DefaultRouter()
# restricoes_router.register(
#     r"av", restricoes_views.AreaVerdeViewSet, basename="av")
# restricoes_router.register(
#     r"corte-av", restricoes_views.CorteAreaVerdeViewSet, basename="corte-av")
# restricoes_router.register(
#     r"margem-rio", restricoes_views.MargemRioViewSet, basename="margem-rio")
# restricoes_router.register(
#     r"margem-lt", restricoes_views.MargemLTViewSet, basename="margem-lt")
# restricoes_router.register(
#     r"margem-ferrovia", restricoes_views.MargemFerroviaViewSet, basename="margem-ferrovia")
# restricoes_router.register(
#     r"ruas", restricoes_views.RuaViewSet, basename="ruas")
# restricoes_router.register(
#     r"al", restricoes_views.AreaLoteavelViewSet, basename="al")


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
    path("projetos/<int:project_id>/restricoes/", restricoes_views.RestricoesCreateAPIView.as_view()),
    path("projetos/<int:project_id>/restricoes/list/", restricoes_views.RestricoesListByProjectAPIView.as_view()),
    path("restricoes/<int:restricoes_id>/geo/", restricoes_views.RestricoesGeoDetailAPIView.as_view()),
]
