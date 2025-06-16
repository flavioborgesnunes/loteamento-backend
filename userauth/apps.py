from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules


class UserauthConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'userauth'

    def ready(self):
        from .models import User
        User._connect_signals()
