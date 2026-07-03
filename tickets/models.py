import os
import uuid
from datetime import timedelta
from functools import cached_property
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


def ticket_upload_path(instance, filename):

    """
    Gera um caminho organizado: tickets/ANO/MES/uuid_nomedoarquivo.ext
    Evita colisão de nomes e diretórios com milhares de arquivos.
    """

    # Se a instância ainda não tem ID (criação), usamos data
    ext = filename.split(".")[-1]
    new_filename = f"{uuid.uuid4().hex[:10]}.{ext}"
    today = timezone.now()
    return f"tickets/{today.year}/{today.month}/{new_filename}"


def interacao_upload_path(instance, filename):

    """
    Organiza anexos do chat: tickets/ID_DO_TICKET/chat/nomedoarquivo
    """

    # Tenta pegar o ID do ticket. Se não existir, usa 'sem_ticket'
    ticket_id = instance.ticket.id if instance.ticket else "temp"
    return f"tickets/{ticket_id}/chat/{filename}"


def interacao_anexo_upload_path(instance, filename):

    """
    Organiza anexos múltiplos do chat: tickets/ID_DO_TICKET/chat/nomedoarquivo
    """

    ticket_id = (
        instance.interacao.ticket.id
        if instance.interacao and instance.interacao.ticket
        else "temp"
    )
    return f"tickets/{ticket_id}/chat/{filename}"


# CONSTANTES DE STATUS (Limpeza Visual)
MAXIMO_STATUS_CHOICES = [
    ("NEW", "Novo"),
    ("QUEUED", "Em fila"),
    ("INPROG", "Em Andamento"),
    ("PENDING", "Pendente"),
    ("RESOLVED", "Resolvido"),
    ("CLOSED", "Fechado"),
    ("CANCELLED", "Cancelado"),
    ("REJECTED", "Rejeitado"),
    ("TSTCLI", "Teste do cliente"),
    ("TSTCLIOK", "Teste do cliente OK"),
    ("TSTCLIFAIL", "Teste do cliente falhou"),
    ("IMPPRODOK", "Implementação em produção OK"),
    ("AGREUN", "Reunião Agendada"),
    ("TREINAMTO", "Treinamento"),
    ("DOC", "Documentar"),
]

PRIORIDADE_CHOICES = [
    ("", "Selecione..."),
    ("1", "1 - Crítica"),
    ("2", "2 - Alta"),
    ("3", "3 - Média"),
    ("4", "4 - Baixa"),
    ("5", "5 - Sem Prioridade"),
]

# Empresas cujo fluxo de abertura usa o campo "Área" (gate por location).
EMPRESAS_COM_AREA = ("PAMPA", "ABL")


# MODELS

class Cliente(AbstractUser):
    location = models.CharField(max_length=200, blank=True, null=True)
    person_id = models.CharField(max_length=150, blank=True, null=True)
    email = models.EmailField(unique=True, verbose_name="Endereço de e-mail")
    
    
    precisa_trocar_senha = models.BooleanField(
        default=True, 
        verbose_name="Precisa trocar senha no próximo login"
    )

    groups = models.ManyToManyField(
        "auth.Group", related_name="cliente_groups", blank=True
    )
    user_permissions = models.ManyToManyField(
        "auth.Permission", related_name="cliente_permissions", blank=True
    )

    class Meta:
        db_table = "clientes"

    @cached_property
    def _nomes_grupos(self) -> set:
        """Nomes dos grupos do usuário, cacheados por instância (1 query por
        request em vez de 1 por acesso a is_consultor/is_lider_suporte).
        Usa groups.all() para aproveitar prefetch_related quando existir."""
        return {g.name for g in self.groups.all()}

    @property
    def is_consultor(self):
        return "Consultores" in self._nomes_grupos

    @property
    def is_support_team(self):
        return self.is_staff

    @property
    def is_lider_suporte(self):
        return "lider_suporte" in self._nomes_grupos

    @property
    def tem_acesso_area(self) -> bool:
        """True se a empresa (location) do usuário usa o campo Área.
        Fonte única do gate — usada pelo TicketForm e pelo detalhe do ticket."""
        loc = (self.location or "").upper()
        return any(empresa in loc for empresa in EMPRESAS_COM_AREA)


class Ambiente(models.Model):

    #cliente = models.ForeignKey(
    #    Cliente, on_delete=models.CASCADE, related_name="ambientes"
    #)
    
    clientes = models.ManyToManyField(
        Cliente, 
        related_name="ambientes",
        verbose_name="Clientes com acesso"
    )
    
    nome_ambiente = models.CharField(max_length=100)
    numero_ativo = models.CharField(max_length=20)

    def __str__(self):
        return f"{self.nome_ambiente} ({self.numero_ativo})"


class Area(models.Model):

    clientes = models.ManyToManyField(
        Cliente,
        related_name="areas",
        verbose_name="Clientes com acesso",
    )
    nome_area = models.CharField(max_length=100)

    def __str__(self):
        return self.nome_area


class Ticket(models.Model):

    # Vínculos
    cliente = models.ForeignKey(
        Cliente, on_delete=models.CASCADE, related_name="tickets"
    )

    ambiente = models.ForeignKey(
        Ambiente, on_delete=models.SET_NULL, null=True, blank=False
    )

    area = models.ForeignKey(Area, on_delete=models.SET_NULL, null=True, blank=True)

    # Dados do Chamado
    sumario = models.CharField(max_length=100, verbose_name="Resumo do Problema")
    descricao = models.TextField(verbose_name="Descrição Detalhada")

    documento_requisicao = models.FileField(
        upload_to=ticket_upload_path,
        null=True, 
        blank=True, 
        verbose_name="Documento de Requisição"
    )

    # Integração Maximo
    maximo_id = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        verbose_name="ID do Chamado (SR)",
        db_index=True,
    )

    status_maximo = models.CharField(
        max_length=20,
        default="NEW",
        choices=MAXIMO_STATUS_CHOICES,
        verbose_name="Status Atual",
    )

    # Rastreia se os anexos da abertura subiram aos DOCLINKS da SR (via REST).
    # True por padrão (sem anexos ou enviados via e-mail fallback); vira False
    # enquanto o upload está pendente/em falha, permitindo retry e visibilidade.
    anexos_sincronizados = models.BooleanField(
        default=True,
        verbose_name="Anexos sincronizados com o Maximo",
    )

    owner = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Proprietário (Maximo ID)"
    )

    # Consultores extras que a liderança designa para acompanhar o ticket.
    # Ganham acesso de leitura+interação (igual ao owner) e recebem as
    # notificações de cada interação. Não altera o owner no Maximo.
    seguidores = models.ManyToManyField(
        Cliente,
        related_name="tickets_seguindo",
        blank=True,
        verbose_name="Seguidores (acompanham o ticket)",
    )

    prioridade = models.CharField(
        max_length=2,
        choices=PRIORIDADE_CHOICES,
        default="",
        verbose_name="Prioridade",
        blank=False,
    )

    # Auditoria
    data_criacao = models.DateTimeField(auto_now_add=True, verbose_name="Aberto em")
    data_atualizacao = models.DateTimeField(
        auto_now=True, verbose_name="Última atualização"
    )

    class Meta:
        ordering = ["-data_criacao"]
        db_table = "tickets"
        verbose_name = "Ticket"
        verbose_name_plural = "Tickets"
        indexes = [
            models.Index(fields=['cliente', 'data_criacao']),
            models.Index(fields=['status_maximo']),
        ]

    def __str__(self):
        return f"Ticket #{self.id} - {self.sumario}"

    @property
    def badge_class(self):

        """Retorna a classe CSS do Bootstrap para o status."""

        status = self.status_maximo
        if status in ["RESOLVED", "TSTCLIOK", "IMPPRODOK", "APPR"]:

            return "bg-success"
        
        elif status in [
            "INPROG",
            "PENDING",
            "APPFML",
            "APPLM",
            "TSTCLI",
            "AGREUN",
            "TREINAMTO",
            "DOC",
            "SLAHOLD",
            "QUEUED",
        ]:
            
            return "bg-warning text-dark"
        
        elif status in ["TSTCLIFAIL", "CRITFAIL", "REJECTED", "ROLLBACK"]:

            return "bg-danger"
        
        elif status in ["CLOSED", "CANCELLED", "HISTEDIT", "DRAFT"]:

            return "bg-secondary"
        
        else:

            return "bg-primary"
    
    @property
    def tem_anexos(self):

        """Retorna True se houver pelo menos um arquivo anexado."""

        return self.anexos.exists()
    
    @property
    def is_closed(self) -> bool:

        """
        Verifica se o ticket está em um estado terminal onde interações não são mais permitidas.
        """

        # Ajuste as strings abaixo conforme estão EXATAMENTE no seu banco/choices
        status_terminais = ['RESOLVED', 'CLOSED', 'CANCELLED']
        return self.status_maximo in status_terminais
        
class TicketAnexo(models.Model):

    """
    Modelo para suportar múltiplos arquivos por Ticket.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(Ticket, related_name='anexos', on_delete=models.CASCADE)
    arquivo = models.FileField(upload_to=ticket_upload_path)
    data_envio = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Anexo do Ticket {self.ticket.id}"

    @property
    def filename(self):
        return os.path.basename(self.arquivo.name)


class TicketInteracao(models.Model):

    ticket = models.ForeignKey(
        Ticket, on_delete=models.CASCADE, related_name="interacoes"
    )

    autor = models.ForeignKey(Cliente, on_delete=models.CASCADE, verbose_name="Autor")

    mensagem = models.TextField(verbose_name="Mensagem")

    # upload_to organizado
    anexo = models.FileField(
        upload_to=interacao_upload_path,
        null=True,
        blank=True,
        verbose_name="Anexo (Opcional)",
    )

    data_criacao = models.DateTimeField(auto_now_add=True)

    editado_em = models.DateTimeField(
        null=True, blank=True, verbose_name="Editado em"
    )

    class Meta:

        ordering = ["data_criacao"]
        db_table = "ticket_interacoes"
        verbose_name = "Interação"
        verbose_name_plural = "Interações"

    def __str__(self):
        return f"Msg de {self.autor.username} em {self.ticket.id}"

    @property
    def is_support(self):
        return self.autor.is_support_team or self.autor.is_lider_suporte

    @property
    def foi_editado(self) -> bool:
        return self.editado_em is not None

    def pode_editar(self, user) -> bool:
        if not user or self.autor_id != user.id:
            return False
        return (timezone.now() - self.data_criacao) <= timedelta(hours=24)

    @property
    def filename(self):
        if self.anexo:
            return os.path.basename(self.anexo.name)
        return None
    
    @property
    def filename_short(self):

        """
        Retorna uma versão encurtada do nome, mantendo o início e a extensão.
        Ex: 'Relatorio_Financeiro_Final_2024.pdf' -> 'Relatorio_Fin...2024.pdf'
        """

        if not self.anexo:
            return None
            
        name = os.path.basename(self.anexo.name)
        
        # Se o nome for menor que 37 caracteres, retorna inteiro
        if len(name) <= 37:
            return name
            
        # Se for maior, pega os primeiros 15, adiciona "..." e pega os últimos 10
        # Isso garante que a extensão (.pptx) sempre apareça
        return f"{name[:30]}...{name[-7:]}"


class InteracaoAnexo(models.Model):

    """
    Suporta múltiplos arquivos por interação do chat.
    O campo legado TicketInteracao.anexo continua válido para leitura.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    interacao = models.ForeignKey(
        TicketInteracao, related_name="anexos", on_delete=models.CASCADE
    )
    arquivo = models.FileField(upload_to=interacao_anexo_upload_path)
    data_envio = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["data_envio"]

    def __str__(self):
        return f"Anexo da interação {self.interacao_id}"

    @property
    def filename(self):
        return os.path.basename(self.arquivo.name)


class Notificacao(models.Model):

    TIPO_CHOICES = (
        ("mensagem", "Nova Mensagem"),
        ("status", "Mudança de Status"),
        ("sistema", "Aviso do Sistema"),
        ("novo_ticket", "Novo Ticket"),
    )

    destinatario = models.ForeignKey(
        Cliente, on_delete=models.CASCADE, related_name="notificacoes"
    )

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, null=True, blank=True)

    titulo = models.CharField(max_length=50, default="Nova Notificação")
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default="sistema")

    mensagem = models.CharField(max_length=255)
    lida = models.BooleanField(default=False)
    data_criacao = models.DateTimeField(auto_now_add=True)
    link = models.CharField(max_length=200, blank=True, null=True)

    class Meta:
        ordering = ["-data_criacao"]

    def __str__(self):
        return f"{self.titulo} - {self.destinatario}"
