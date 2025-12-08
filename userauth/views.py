import shortuuid
from decouple import config
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import send_mail
from django.db.models import Q
from rest_framework import generics, permissions, serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from .models import User
from .permissions import IsDonoOrAdm
from .serializers import (MyTokenObtainPairSerializer, PasswordResetSerializer,
                          RegisterUserSerializer, UserSerializer,
                          UserUpdateSerializer)


class UserView(generics.RetrieveAPIView):
    serializer_class = UserSerializer
    permission_classes = (IsAuthenticated, )

    def get_object(self):
        return self.request.user


class UserUpdateView(generics.UpdateAPIView):
    serializer_class = UserUpdateSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


class MyTokenObtainPairView(TokenObtainPairView):
    serializer_class = MyTokenObtainPairSerializer


class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    permission_classes = (AllowAny, )
    serializer_class = RegisterUserSerializer


class PasswordResetEmailVerify(generics.RetrieveAPIView):
    permission_classes = (AllowAny,)
    serializer_class = UserSerializer

    def get_object(self):
        email = self.kwargs['email']
        try:
            user = User.objects.get(email=email)
        except ObjectDoesNotExist:
            raise serializers.ValidationError("E-mail não encontrado.")

        user.otp = shortuuid.uuid()[:6]
        user.save()

        frontend_url = config("FRONTEND_URL", default="http://localhost:5173")

        link = f"{frontend_url}/create-new-password?otp={user.otp}&uidb64={user.pk}"

        send_mail(
            subject='Redefinição de senha',
            message=f'Clique no link para redefinir sua senha: {link}',
            from_email='no-reply@lotenet.com',
            recipient_list=[email],
            fail_silently=False,
        )

        return user


class PasswordChangeView(generics.CreateAPIView):
    def create(self, request, *args, **kwargs):
        payload = request.data
        otp = payload['otp']
        uidb64 = payload['uidb64']
        password = payload['password']

        try:
            validate_password(password)  # Validação aqui também

            user = User.objects.get(id=uidb64, otp=otp)
            user.set_password(password)
            user.otp = ""
            user.save()
            return Response({"message": "Senha redefinida com sucesso."}, status=status.HTTP_201_CREATED)
        except User.DoesNotExist:
            return Response({"message": "Erro ao redefinir senha."}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"message": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class UsuariosDoDonoListView(generics.ListAPIView):
    """
    Retorna todos os usuários do mesmo dono do usuário logado,
    incluindo o próprio dono.
    """
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user

        # Descobre quem é o "dono base"
        if getattr(user, "role", None) == "dono":
            dono_base = user
        elif getattr(user, "dono_id", None):
            dono_base = user.dono
        else:
            # sem dono vinculado → devolve só ele mesmo
            return User.objects.filter(id=user.id)

        return (
            User.objects.filter(
                Q(dono=dono_base) | Q(id=dono_base.id)
            )
            .order_by("role", "nome", "email")
        )
