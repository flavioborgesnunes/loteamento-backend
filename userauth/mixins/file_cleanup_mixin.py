from django.db.models.signals import post_delete, pre_save
from django.dispatch import receiver


class FileCleanupMixin:
    """
    Mixin para remoção automática de arquivos em atualização e exclusão.
    Deve ser usado com models que possuem FileField ou ImageField.
    """

    @classmethod
    def _connect_signals(cls):
        pre_save.connect(cls._delete_old_files_on_update, sender=cls)
        post_delete.connect(cls._delete_files_on_delete, sender=cls)

    @staticmethod
    def _delete_file(fieldfile):
        if fieldfile and hasattr(fieldfile, 'delete'):
            try:
                fieldfile.delete(save=False)
            except Exception as e:
                # Não quebra a app se falhar (ex: arquivo já foi removido do bucket)
                print(f'Erro ao deletar arquivo: {e}')

    @classmethod
    def _delete_old_files_on_update(cls, sender, instance, **kwargs):
        if not instance.pk:
            return  # novo objeto, sem arquivo anterior

        try:
            old_instance = sender.objects.get(pk=instance.pk)
        except sender.DoesNotExist:
            return

        for field in instance._meta.fields:
            if hasattr(field, 'upload_to'):
                old_file = getattr(old_instance, field.name)
                new_file = getattr(instance, field.name)
                if old_file and old_file != new_file:
                    cls._delete_file(old_file)

    @classmethod
    def _delete_files_on_delete(cls, sender, instance, **kwargs):
        for field in instance._meta.fields:
            if hasattr(field, 'upload_to'):
                file = getattr(instance, field.name)
                cls._delete_file(file)
