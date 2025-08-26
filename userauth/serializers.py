import shortuuid
from decouple import config
from django.contrib.auth.password_validation import validate_password
from django.core.mail import send_mail
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import User


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    foto = serializers.ImageField(use_url=True)

    class Meta:
        model = User
        fields = [
            'id',
            'email',
            'nome',
            'sobrenome',
            'foto',
            'role',
            'dono',
            'otp',
            'is_active',
            'full_name',
        ]
        read_only_fields = ['otp', 'dono', 'role', 'is_active', 'full_name']

    def get_full_name(self, obj):
        return obj.get_full_name()


class UserUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['nome', 'sobrenome', 'foto']


class RegisterUserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)
    password2 = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ['email', 'password', 'password2', 'role', 'dono']
        extra_kwargs = {'dono': {'read_only': True}}

    def validate(self, attrs):
        if attrs['password'] != attrs['password2']:
            raise serializers.ValidationError(
                {"password": "As senhas não coincidem."})
        validate_password(attrs['password'])

        request_user = self.context.get(
            'request').user if self.context.get('request') else None
        desired_role = attrs.get('role')

        if request_user and request_user.is_authenticated:
            # Dono/adm criando usuários
            if request_user.role not in ['dono', 'adm']:
                raise serializers.ValidationError(
                    "Sem permissão para criar usuários.")

            # Regra de quem pode criar o quê:
            if request_user.role == 'dono':
                if desired_role not in ['adm', 'comum']:
                    raise serializers.ValidationError(
                        "Dono só pode criar usuários 'adm' ou 'comum'.")
            elif request_user.role == 'adm':
                if desired_role not in ['comum', 'adm']:
                    # Se quiser restringir 'adm' por adm, troque para only 'comum'
                    raise serializers.ValidationError(
                        "Administrador só pode criar usuários 'comum' ou 'adm'.")

        else:
            # Público (não autenticado) só pode se registrar como 'dono'
            if desired_role != 'dono':
                raise serializers.ValidationError(
                    "Apenas clientes podem se registrar diretamente como 'dono'.")

        return attrs

    def create(self, validated_data):
        request_user = self.context.get(
            'request').user if self.context.get('request') else None

        password = validated_data.pop('password')
        validated_data.pop('password2')

        if request_user and request_user.is_authenticated:
            if request_user.role not in ['dono', 'adm']:
                raise serializers.ValidationError(
                    "Sem permissão para criar usuários.")

            if request_user.role == 'dono':
                validated_data['dono'] = request_user
            elif request_user.role == 'adm':
                validated_data['dono'] = request_user.dono
        else:
            # Registro público: só 'dono'
            if validated_data.get('role') != 'dono':
                raise serializers.ValidationError(
                    "Apenas clientes podem se registrar diretamente como 'dono'.")
            validated_data['dono'] = None

        user = User(**validated_data)
        user.set_password(password)
        user.otp = shortuuid.uuid()[:6]
        user.save()

        # Enviar link de criação de senha por e-mail
        frontend_url = config('CORS_ALLOWED_ORIGINS').split(',')[0]
        link = f"{frontend_url}/create-new-password?otp={user.otp}&uidb64={user.pk}"

        send_mail(
            subject="Bem-vindo ao Lotenet - Crie sua senha",
            message=f"Você foi cadastrado no sistema.\n\nAcesse: {link} para definir sua senha.",
            from_email="no-reply@lotenet.com",
            recipient_list=[user.email],
            fail_silently=False,
        )

        return user


class MyTokenObtainPairSerializer(TokenObtainPairSerializer):
    username_field = 'email'

    def validate(self, attrs):
        attrs['username'] = attrs.get('email')
        return super().validate(attrs)

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token['email'] = user.email
        token['role'] = user.role
        token['dono'] = user.dono_id
        return token


class PasswordResetSerializer(serializers.Serializer):
    otp = serializers.CharField()
    uidb64 = serializers.IntegerField()
    password = serializers.CharField(write_only=True)

    def validate(self, data):
        try:
            user = User.objects.get(id=data['uidb64'], otp=data['otp'])
        except User.DoesNotExist:
            raise serializers.ValidationError("Link inválido ou expirado.")
        return data

    def save(self, **kwargs):
        user = User.objects.get(
            id=self.validated_data['uidb64'], otp=self.validated_data['otp'])
        user.set_password(self.validated_data['password'])
        user.otp = ''
        user.save()
        return user
