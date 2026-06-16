import logging
import time
from typing import Callable

from django.http import HttpRequest, HttpResponse

logger = logging.getLogger("portal.http")

# Prefixos ignorados (ruído de assets)
PREFIXOS_IGNORADOS = ("/static/", "/media/")


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

        usuario = getattr(getattr(request, "user", None), "username", None) or "anon"
        logger.info(
            f"{request.method} {request.path} user={usuario} "
            f"{response.status_code} {duracao_ms}ms"
        )
        return response
