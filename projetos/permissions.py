# permissions.py
from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsOwnerOrDonoOrReadOnly(BasePermission):
    """
    Leitura (SAFE_METHODS): qualquer usuário autenticado pode ver qualquer projeto.
    Edição/Exclusão: só superuser/staff, ou dono do tenant, ou owner do projeto.
    """

    def has_permission(self, request, view):
        # exige login para qualquer ação; libera leitura para todos logados
        return request.user and request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        # leitura sempre permitida para autenticados
        if request.method in SAFE_METHODS:
            return True

        user = request.user
        if user.is_superuser or user.is_staff:
            return True

        # dono do tenant pode editar projetos do próprio tenant
        if getattr(obj, "dono_id", None) and user.role == "dono" and obj.dono_id == user.id:
            return True

        # owner do projeto pode editar o próprio projeto
        if getattr(obj, "owner_id", None) and obj.owner_id == user.id:
            return True

        return False

        return False
        return False
