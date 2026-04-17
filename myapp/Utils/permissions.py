"""Role-based permission classes."""
from rest_framework.permissions import BasePermission, SAFE_METHODS
from myapp.Models.Auth_models import UserRole


class IsAdmin(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.role == UserRole.ADMIN)


class IsAccountant(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.role == UserRole.ACCOUNTANT)


class IsCustomer(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.role == UserRole.CUSTOMER)


class IsAdminOrAccountant(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(
            u and u.is_authenticated
            and u.role in (UserRole.ADMIN, UserRole.ACCOUNTANT)
        )


class IsOwnerOrStaff(BasePermission):
    """
    Customers can only access their own objects. Admin/Accountant can access all.
    Expects the object to have either `.customer` or `.user` FK.
    """
    def has_object_permission(self, request, view, obj):
        u = request.user
        if not (u and u.is_authenticated):
            return False
        if u.role in (UserRole.ADMIN, UserRole.ACCOUNTANT):
            return True
        owner = getattr(obj, "customer", None) or getattr(obj, "user", None)
        return owner == u


class ReadOnlyForCustomer(BasePermission):
    """
    Allow customers read-only access. Admin/accountant full access.
    """
    def has_permission(self, request, view):
        u = request.user
        if not (u and u.is_authenticated):
            return False
        if u.role in (UserRole.ADMIN, UserRole.ACCOUNTANT):
            return True
        return request.method in SAFE_METHODS
