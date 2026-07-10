from functools import wraps

from django.http import HttpResponse, JsonResponse
from django_ratelimit.decorators import ratelimit

MSG_429 = "Muitas requisições. Aguarde um momento."

# Rates por categoria (requisições/min por usuário autenticado)
RATE_CRIAR = "5/m"          # criar ticket: Maximo REST + e-mail + upload
RATE_MSG = "20/m"           # enviar mensagem no chat
RATE_EDITAR = "20/m"        # editar interação
RATE_DOWNLOAD = "40/m"      # baixar anexo (banda)
RATE_FILTRO = "30/m"        # listar/filtrar tickets (carga DB)
RATE_NOTIF_TODAS = "20/m"   # marcar todas notificações lidas
RATE_GERENCIAR = "10/m"     # seguidores/colegas
RATE_GLOBAL = "120/m"       # rede global (middleware)


def resposta_429(request) -> HttpResponse:
    """Resposta 429 no formato certo: JSON para AJAX, texto para navegação."""
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"status": "error", "message": MSG_429}, status=429)
    return HttpResponse(MSG_429, status=429, content_type="text/plain; charset=utf-8")


def throttle(rate: str, method: str = "POST"):
    """Aplica django-ratelimit (block=False) e devolve 429 unificado no estouro.
    Abaixo do limite, delega para a view normalmente."""
    def deco(view):
        @ratelimit(key="user", rate=rate, method=method, block=False)
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if getattr(request, "limited", False):
                return resposta_429(request)
            return view(request, *args, **kwargs)
        return wrapped
    return deco
