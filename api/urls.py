from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
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

]
