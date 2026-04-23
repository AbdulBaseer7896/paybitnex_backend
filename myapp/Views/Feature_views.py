"""
Feature-gating views.

Public:
  GET  /core/features/                  → registry (auth required)

Admin:
  GET  /accounts/users/<id>/features/   → read one user's feature map
  PATCH/PUT /accounts/users/<id>/features/ → update that map

The registry endpoint is deliberately reachable by any authenticated
user (not just admins) so the customer portal can, in future, display
"locked" feature tiles with proper labels/descriptions.
"""
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from django.shortcuts import get_object_or_404

from myapp.Models.Auth_models import User, UserRole
from myapp.Models.Audit_models import AuditLog
from myapp.Utils.permissions import IsAdmin
from myapp.Utils.features import (
    FEATURES, feature_registry_for_api,
    user_feature_map, set_user_features,
)


class FeatureRegistryView(APIView):
    """GET /core/features/ — list every defined feature."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"features": feature_registry_for_api()})


class UserFeaturesView(APIView):
    """
    Admin-only endpoint to read and update a specific user's premium
    feature grants.

    GET returns {features: {key: bool}} plus the registry so the UI
    can render every feature even if the user has no rows yet.

    PATCH accepts {features: {key: bool, ...}} and creates/updates the
    CustomerFeatureAccess rows accordingly. Only customers can be
    modified through this endpoint — toggling features for admins or
    accountants is meaningless (staff always pass all gates).
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        return Response({
            "user_id": str(user.id),
            "email": user.email,
            "role": user.role,
            "features": user_feature_map(user),
            "registry": feature_registry_for_api(),
        })

    def patch(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)

        if user.role != UserRole.CUSTOMER:
            return Response(
                {"detail": "Feature grants only apply to customer accounts."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        updates = request.data.get("features") or {}
        if not isinstance(updates, dict):
            return Response(
                {"detail": "`features` must be an object of {key: bool}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Ignore unknown keys silently so typos on the admin side don't
        # 500; they just become no-ops.
        known = {k: bool(v) for k, v in updates.items() if k in FEATURES}
        unknown = [k for k in updates.keys() if k not in FEATURES]

        before = user_feature_map(user)
        notes = request.data.get("notes", "") or ""

        after = set_user_features(
            user, known,
            granted_by=request.user,
            notes=notes,
        )

        # Audit trail — useful when a customer asks "why can't I see invoices?"
        changed = {
            k: {"before": before.get(k), "after": after.get(k)}
            for k in known
            if before.get(k) != after.get(k)
        }
        if changed:
            AuditLog.record(
                user=request.user, action=AuditLog.ACTION_UPDATE, target=user,
                description=(
                    f"Admin updated feature access for {user.email}: "
                    + ", ".join(f"{k}={v['after']}" for k, v in changed.items())
                ),
                before={"features": before},
                after={"features": after},
            )

        payload = {
            "user_id": str(user.id),
            "email": user.email,
            "features": after,
        }
        if unknown:
            payload["ignored_keys"] = unknown
        return Response(payload)

    # Many frontends prefer PUT for a full replacement — accept it too.
    def put(self, request, user_id):
        return self.patch(request, user_id)
