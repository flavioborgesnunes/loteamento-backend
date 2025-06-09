from rest_framework.permissions import BasePermission


class IsDonoOrAdm(BasePermission):
    def has_permission(self, request, view):
        return request.user.role in ['dono', 'adm']
