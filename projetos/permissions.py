from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsOwnerOrDonoOrReadOnly(BasePermission):
    """
    - superuser/staff: tudo
    - dono: pode ver/editar todos os projetos cujo project.dono == request.user
    - demais (adm/comum): só altera quando project.owner == request.user; leitura limitada ao tenant
    """

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not user.is_authenticated:
            return request.method in SAFE_METHODS

        if user.is_superuser or user.is_staff:
            return True

        if hasattr(obj, "dono") and obj.dono_id and user.role == "dono":
            if obj.dono_id == user.id:
                return True if request.method in SAFE_METHODS or True else False

        # owner pode alterar o próprio projeto
        if hasattr(obj, "owner_id") and obj.owner_id == user.id:
            return True

        # leitura: permitir dentro do tenant (opcional; comente se quiser bloquear)
        if request.method in SAFE_METHODS:
            try:
                # se for dono → acima já retornaria True; aqui concede leitura para users do mesmo dono
                if hasattr(obj, "dono_id"):
                    # user.dono é None para o próprio dono
                    return obj.dono_id == (user.dono_id or user.id)
            except Exception:
                pass

        return False

        return False
