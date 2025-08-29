# api/urls.py  ← SUBSTITUA TODO O ARQUIVO POR ESTE

from django.urls import include, path
from geodata import views as geodata_views
from ia import views as ia_views
from projetos.views import (exportar_projeto, list_projects,
                            project_features_geojson, project_map_summary,
                            project_overlay_delete, project_overlay_patch,
                            update_delete_project)
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView
from rios import views as rios_views
# Apps existentes
from userauth import views as userauths_views

# Projetos (tudo vem direto do app projetos)
# from projetos.views import (ProjectViewSet, exportar_projeto,
#                             projeto_features_geojson, projeto_map_summary,
#                             projeto_overlay_delete, projeto_overlay_update)


# router = DefaultRouter()
# ViewSet de projetos (CRUD)
# router.register(r"projetos", ProjectViewSet, basename="projetos")

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
    # path("openai/", ia_views.LegalQueryView.as_view(), name="openai_query"),
    path("autofill/", ia_views.LegalAutoFillView.as_view(), name="autofill"),

    # Rios
    path("rios/geojson/", rios_views.rios_geojson, name="rios-geojson"),
    path("export/rios/", rios_views.export_rios_kmz, name="export-rios-kmz"),

    # Geodata (legado). Se quiser, mantenha para compatibilidade;
    # Caso vá usar só o fluxo de projetos, você pode remover esta rota:
    path("export/mapa/", geodata_views.export_mapa_kmz, name="export-mapa-kmz"),

    # Projetos
    path("projetos/", list_projects),  # GET /projetos/
    path("projetos/<int:pk>/", update_delete_project),  # PATCH/DELETE
    path("projetos/<int:pk>/map/summary/", project_map_summary),
    path("projetos/<int:pk>/features/", project_features_geojson),
    path("projetos/<int:pk>/overlay/", project_overlay_patch),
    path("projetos/<int:pk>/overlay/delete/", project_overlay_delete),
    path("projetos/exportar/", exportar_projeto),

]
