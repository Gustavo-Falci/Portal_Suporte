from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.urls import reverse
from unittest.mock import patch
from .models import Ticket, TicketInteracao, Ambiente
from .services import MaximoSenderService

Cliente = get_user_model()

class TicketModelTests(TestCase):
    """
    Testes focados na lógica de negócio das Entidades (Models).
    """
    def setUp(self):
        self.user = Cliente.objects.create(email="cliente@teste.com", username="cliente_teste")
        self.ticket = Ticket.objects.create(
            cliente=self.user,
            sumario="Erro de Acesso",
            descricao="Não consigo logar no sistema",
            status_maximo="NEW",
            prioridade="3"
        )

    def test_ticket_is_closed_property(self):
        """Garante que a propriedade is_closed reconheça os status terminais."""
        # Status inicial é NEW, não deve estar fechado
        self.assertFalse(self.ticket.is_closed)

        # Muda para status terminal e verifica novamente
        self.ticket.status_maximo = "CLOSED"
        self.ticket.save()
        self.assertTrue(self.ticket.is_closed)

        self.ticket.status_maximo = "RESOLVED"
        self.ticket.save()
        self.assertTrue(self.ticket.is_closed)


class MaximoServiceTests(TestCase):
    """
    Testes da camada de Serviço que integra com a API do IBM Maximo.
    Usamos @patch para interceptar o 'requests.post' e não depender da internet.
    """
    def setUp(self):
        self.user = Cliente.objects.create(email="autor@teste.com", username="autor_teste")
        self.ticket = Ticket.objects.create(
            cliente=self.user,
            sumario="Problema ERP",
            descricao="Trava ao salvar",
            maximo_id="SR102030" # Obrigatório para o envio do Worklog funcionar
        )
        self.interacao = TicketInteracao.objects.create(
            ticket=self.ticket,
            autor=self.user,
            mensagem="Teste de envio para o Maximo"
        )

    @patch('tickets.services.requests.post')
    def test_enviar_interacao_sucesso(self, mock_post):
        """Testa o cenário onde o Maximo responde com Sucesso (200)."""
        # Configuramos o "Mock" (Dublê) para fingir que a API retornou Status 200
        mock_post.return_value.status_code = 200
        
        sucesso = MaximoSenderService.enviar_interacao(self.ticket, self.interacao)
        
        self.assertTrue(sucesso)
        mock_post.assert_called_once() # Garante que o requests.post foi chamado

    @patch('tickets.services.requests.post')
    def test_enviar_interacao_falha_api(self, mock_post):
        """Testa o comportamento do sistema quando a API do Maximo cai (500)."""
        # Simulamos uma queda no servidor do Maximo
        mock_post.return_value.status_code = 500
        mock_post.return_value.text = "Internal Server Error"
        
        sucesso = MaximoSenderService.enviar_interacao(self.ticket, self.interacao)
        
        # A função deve tratar graciosamente o erro e retornar False
        self.assertFalse(sucesso)

    def test_enviar_interacao_sem_maximo_id(self):
        """Testa a rejeição imediata se o Ticket local ainda não tiver vínculo com Maximo."""
        self.ticket.maximo_id = None
        self.ticket.save()

        sucesso = MaximoSenderService.enviar_interacao(self.ticket, self.interacao)
        self.assertFalse(sucesso)


class SecurityViewsTests(TestCase):
    """
    Testes para garantir as regras de ACL (Access Control List).
    """
    def setUp(self):
        self.client = Client()
        
        # Cria o Dono do Ticket
        self.dono = Cliente.objects.create_user(email="dono@teste.com", username="dono", password="123")
        self.dono.precisa_trocar_senha = False
        self.dono.save()

        # Cria um usuário "Invasor" (Outro cliente do portal)
        self.invasor = Cliente.objects.create_user(email="invasor@teste.com", username="invasor", password="123")
        self.invasor.precisa_trocar_senha = False
        self.invasor.save()

        self.ticket = Ticket.objects.create(cliente=self.dono, sumario="Privado", descricao="Dados sensíveis")

    def test_acesso_negado_ticket_de_terceiro(self):
        """Garante que um cliente não consiga ver a URL de detalhes do ticket de outro."""
        self.client.login(email="invasor@teste.com", password="123") # O backend espera 'email' agora
        response = self.client.get(reverse('tickets:detalhe_ticket', kwargs={'pk': self.ticket.pk}))
        
        # Deve ser redirecionado para a lista de tickets (Código 302 Found) e não acessar o chamado
        self.assertRedirects(response, reverse('tickets:meus_tickets'))
