from django.http import HttpRequest
from .models import Cliente, ModoManutencao, Notificacao


def dados_notificacoes(user: Cliente) -> dict:
    """
    Contagem real de não-lidas + as 5 mais recentes para o dropdown do sino.
    Compartilhado entre o context processor (render de página) e o endpoint
    de polling AJAX (views.notificacoes_badge).
    """
    # 1. QuerySet Base: Todas as notificações não lidas deste usuário
    qs_nao_lidas = Notificacao.objects.filter(destinatario=user, lida=False)

    # 2. Contagem Real: Conta no banco o total (Ex: 15), antes de cortar
    qtd_total = qs_nao_lidas.count()

    # 3. Lista para o Dropdown: Pega apenas as 5 mais recentes
    # O slice [:5] deve ser feito APÓS a contagem total
    ultimas_notificacoes = qs_nao_lidas.order_by("-data_criacao")[:5]

    return {
        "notificacoes_list": ultimas_notificacoes,
        "notificacoes_count": qtd_total,  # Mostra o número real (ex: 15) e não apenas 5
    }


def notificacoes_usuario(request: HttpRequest) -> dict:

    """
    Disponibiliza as notificações em todos os templates.
    """
    if request.user.is_authenticated:
        return dados_notificacoes(request.user)

    return {}


def modo_manutencao(request: HttpRequest) -> dict:

    """
    Disponibiliza o aviso de manutenção em todos os templates.

    Roda também para anônimo (de propósito): o aviso precisa aparecer na tela
    de login. Sai `None` quando desligado — 1 query barata por página.
    """
    manutencao = ModoManutencao.objects.filter(pk=1, ativo=True).first()
    return {"manutencao": manutencao}
