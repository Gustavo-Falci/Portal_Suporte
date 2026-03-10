from django.http import HttpRequest
from .models import Notificacao


def notificacoes_usuario(request: HttpRequest) -> dict:

    """
    Disponibiliza as notificações em todos os templates.
    """
    if request.user.is_authenticated:
        
        # 1. QuerySet Base: Todas as notificações não lidas deste usuário
        qs_nao_lidas = Notificacao.objects.filter(destinatario=request.user, lida=False)

        # 2. Contagem Real: Conta no banco o total (Ex: 15), antes de cortar
        qtd_total = qs_nao_lidas.count()

        # 3. Lista para o Dropdown: Pega apenas as 5 mais antigos
        # O slice [:5] deve ser feito APÓS a contagem total
        ultimas_notificacoes = qs_nao_lidas.order_by("data_criacao")[:5]

        return {
            "notificacoes_list": ultimas_notificacoes,
            "notificacoes_count": qtd_total,  # Agora mostra o número real (ex: 15) e não apenas 5
        }

    return {}
