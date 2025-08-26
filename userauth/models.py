import shortuuid
from django.contrib.auth.models import (AbstractBaseUser, BaseUserManager,
                                        PermissionsMixin)
from django.db import models

from .mixins.file_cleanup_mixin import FileCleanupMixin


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, role='comum', dono=None, **extra_fields):
        if not email:
            raise ValueError("O campo email é obrigatório")
        email = self.normalize_email(email)

        # Segurança: impedir que alguém crie user comum já com superpoderes
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)

        user = self.model(email=email, role=role, dono=dono, **extra_fields)
        user.set_password(password)
        user.save()
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        # Superuser é um operador da plataforma, NÃO um 'dono' do seu produto
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser precisa ter is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser precisa ter is_superuser=True.')

        # Não force 'role' aqui; deixe o default 'comum' ou use 'adm' se preferir
        return self.create_user(email, password, **extra_fields)


class User(FileCleanupMixin, AbstractBaseUser, PermissionsMixin):
    ROLE_CHOICES = (
        ('dono', 'Dono'),
        ('adm', 'Administrador'),
        ('comum', 'Usuário Comum'),
    )

    email = models.EmailField(unique=True)
    nome = models.CharField(max_length=150, blank=True, null=True)
    sobrenome = models.CharField(max_length=150, blank=True, null=True)
    foto = models.ImageField(
        upload_to='usuarios/fotos/', blank=True, null=True)

    role = models.CharField(
        max_length=10, choices=ROLE_CHOICES, default='comum')
    dono = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE,
                             related_name='usuarios', limit_choices_to={'role': 'dono'})

    otp = models.CharField(max_length=10, blank=True, null=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = UserManager()

    def __str__(self):
        return self.get_full_name()

    def get_full_name(self):
        if self.nome:
            return f"{self.nome} {self.sobrenome or ''}".strip()
        return self.email
