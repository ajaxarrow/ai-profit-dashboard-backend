from django.conf import settings
from django.http import JsonResponse

# Writes to stores/credentials/imports. Chat, the monitor, notes and approving signals stay available.
BLOCKED_PREFIX = "/api/stores/"


class ReadOnlyMiddleware:
    """In READ_ONLY mode (public demo) nobody can connect, sync, import into, edit or delete a store."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.READ_ONLY and request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith(BLOCKED_PREFIX):
            return JsonResponse({"error": "This is a read-only demo. Connecting, syncing and importing are turned off."}, status=403)
        return self.get_response(request)
