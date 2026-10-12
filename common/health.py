from django.db import connections
from django.http import JsonResponse, HttpResponseServerError
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods


@csrf_exempt
@require_http_methods(["GET"])
def health_check(request):
    """
    Database-aware health check endpoint.
    
    Returns:
        - 200 OK with status 'ok' and db 'ok' if database is reachable
        - 500 Server Error with status 'error' and db 'down' if database is unreachable
    """
    try:
        db = connections["default"]
        db.ensure_connection()  # Raises if database is unreachable
        return JsonResponse({"status": "ok", "db": "ok"})
    except Exception:
        return HttpResponseServerError(
            JsonResponse({"status": "error", "db": "down"}).content,
            content_type="application/json",
        )
