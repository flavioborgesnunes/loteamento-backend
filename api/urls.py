from django.urls import path
from geodata import views as geodata_views
from ia import views as ia_views
from rest_framework_simplejwt.views import TokenRefreshView
from rios import views as rios_views
from userauth import views as userauths_views

urlpatterns = [
    path('user/', userauths_views.UserView.as_view()),
    path('user/update/', userauths_views.UserUpdateView.as_view()),

    path('user/token/', userauths_views.MyTokenObtainPairView.as_view()),
    path('user/token/refresh/', TokenRefreshView.as_view()),
    path('user/register/', userauths_views.RegisterView.as_view()),
    path('user/password-reset/<email>/',
         userauths_views.PasswordResetEmailVerify.as_view()),
    path('user/password-change/', userauths_views.PasswordChangeView.as_view()),

    # IA
    # path("openai/", ia_views.LegalQueryView.as_view(), name="openai_query"),
    path("autofill/", ia_views.LegalAutoFillView.as_view(), name="autofill"),

    # Rios
    path("rios/geojson/", rios_views.rios_geojson, name="rios-geojson"),
    path("export/rios/", rios_views.export_rios_kmz, name="export-rios-kmz"),

    # Geodata
    path("export/mapa/", geodata_views.export_mapa_kmz,
         name="export-mapa-kmz"),  # NOVO



]
