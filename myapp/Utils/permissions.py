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


def HasFeature(feature_key):
    """
    Factory that returns a permission class gating access by feature flag.

    Usage:
        permission_classes = [IsAuthenticated, HasFeature("invoicing")]

    Admins and accountants always pass. Customers pass only if an
    explicit CustomerFeatureAccess row with enabled=True exists for
    them (or the feature has default_enabled=True in the registry).
    """
    from myapp.Utils.features import user_has_feature

    class _HasFeature(BasePermission):
        # DRF serialises the message to the 403 response body.
        message = (
            f"This feature is not available on your account. "
            f"Please contact your administrator to request access."
        )

        def has_permission(self, request, view):
            u = request.user
            if not (u and u.is_authenticated):
                return False
            if u.role in (UserRole.ADMIN, UserRole.ACCOUNTANT):
                return True
            return user_has_feature(u, feature_key)

        def has_object_permission(self, request, view, obj):
            # Same rule at object level — prevents a customer who had
            # access revoked from continuing to operate on old rows.
            return self.has_permission(request, view)

    _HasFeature.__name__ = f"HasFeature_{feature_key}"
    return _HasFeature
