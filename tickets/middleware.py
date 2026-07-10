import logging
import time
from typing import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django_ratelimit import ALL
from django_ratelimit.core import is_ratelimited

from .throttle import RATE_GLOBAL, resposta_429

logger = logging.getLogger("portal.http")

# Prefixos ignorados (ruído de assets)
PREFIXOS_IGNORADOS = ("/static/", "/media/")

# Permissions-Policy: desliga APIs do browser que o portal não usa.
# Reduz superfície caso um XSS tente acessar câmera/microfone/geolocalização.
PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), payment=(), usb=()"
)


class RequestLogMiddleware:
    """Loga 1 linha por requisição: método, caminho, usuário, status, duração.

    Grava apenas metadados — nunca corpo, form data, senha ou token.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path.startswith(PREFIXOS_IGNORADOS):
            return self.get_response(request)

        inicio = time.monotonic()
        response = self.get_response(request)
        duracao_ms = int((time.monotonic() - inicio) * 1000)

        # Header não coberto nativamente pelo Django nem pelo django-csp
        response.setdefault("Permissions-Policy", PERMISSIONS_POLICY)

        usuario = getattr(getattr(request, "user", None), "username", None) or "anon"
        logger.info(
            f"{request.method} {request.path} user={usuario} "
            f"{response.status_code} {duracao_ms}ms"
        )
        return response


class GlobalThrottleMiddleware:
    """Rede de segurança anti-flood: teto frouxo por usuário autenticado em
    toda requisição. Camada acima dos @throttle por-view (mais rígidos nos
    endpoints caros). Pula anônimos, estáticos, media e /logs/ (SSE)."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if self._pular(request):
            return self.get_response(request)
        limitado = is_ratelimited(
            request, group="global", key="user", rate=RATE_GLOBAL,
            method=ALL, increment=True,
        )
        if limitado:
            return resposta_429(request)
        return self.get_response(request)

    def _pular(self, request: HttpRequest) -> bool:
        if not getattr(request, "user", None) or not request.user.is_authenticated:
            return True
        p = request.path
        return (
            p.startswith(settings.STATIC_URL)
            or bool(settings.MEDIA_URL) and p.startswith(settings.MEDIA_URL)
            or p.startswith("/logs/")
        )
