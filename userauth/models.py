import shortuuid
from django.contrib.auth.models import (AbstractBaseUser, BaseUserManager,
                                        PermissionsMixin)
from django.db import models


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, role='comum', dono=None, **extra_fields):
        if not email:
            raise ValueError("O campo email é obrigatório")
        email = self.normalize_email(email)
        user = self.model(email=email, role=role, dono=dono, **extra_fields)
        user.set_password(password)
        user.save()
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email, password, role='dono', **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    ROLE_CHOICES = (
        ('dono', 'Dono'),
        ('adm', 'Administrador'),
        ('comum', 'Usuário Comum'),
    )

    email = models.EmailField(unique=True)
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
        return self.email
