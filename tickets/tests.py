from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.urls import reverse
from unittest.mock import patch
from .models import Ticket, TicketInteracao, Ambiente
from .services import MaximoSenderService
from tickets.views import _tickets_visiveis_cliente, _usuario_tem_acesso_ticket

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


class VisaoEquipeLocationTests(TestCase):
    """Visão compartilhada de chamados por Cliente.location."""

    def setUp(self):
        self.client = Client()
        # Empresa Pampa: dois usuários
        self.ana = Cliente.objects.create_user(
            email="ana@pampa.com", username="ana", password="123", location="PAMPA"
        )
        self.bruno = Cliente.objects.create_user(
            email="bruno@pampa.com", username="bruno", password="123", location="pampa"
        )
        # Empresa diferente
        self.carla = Cliente.objects.create_user(
            email="carla@abl.com", username="carla", password="123", location="ABL"
        )
        # Sem empresa (location null)
        self.diego = Cliente.objects.create_user(
            email="diego@x.com", username="diego", password="123"
        )
        for u in (self.ana, self.bruno, self.carla, self.diego):
            u.precisa_trocar_senha = False
            u.save()

        self.t_ana = Ticket.objects.create(cliente=self.ana, sumario="A", descricao="d")
        self.t_bruno = Ticket.objects.create(cliente=self.bruno, sumario="B", descricao="d")
        self.t_carla = Ticket.objects.create(cliente=self.carla, sumario="C", descricao="d")
        self.t_diego = Ticket.objects.create(cliente=self.diego, sumario="D", descricao="d")

    def test_ve_tickets_da_mesma_location_case_insensitive(self):
        visiveis = _tickets_visiveis_cliente(self.ana)
        self.assertIn(self.t_ana, visiveis)
        self.assertIn(self.t_bruno, visiveis)  # colega "pampa" minúsculo
        self.assertNotIn(self.t_carla, visiveis)

    def test_sem_location_ve_apenas_proprios(self):
        visiveis = _tickets_visiveis_cliente(self.diego)
        self.assertIn(self.t_diego, visiveis)
        self.assertNotIn(self.t_ana, visiveis)
        self.assertEqual(visiveis.count(), 1)

    def test_locations_distintas_nao_se_enxergam(self):
        self.assertNotIn(self.t_ana, _tickets_visiveis_cliente(self.carla))

    def test_permissao_concede_mesma_location(self):
        self.assertTrue(_usuario_tem_acesso_ticket(self.bruno, self.t_ana))

    def test_permissao_nega_location_diferente(self):
        self.assertFalse(_usuario_tem_acesso_ticket(self.carla, self.t_ana))

    def test_permissao_nega_terceiro_sem_location(self):
        # diego (sem location) não acessa ticket de terceiro
        self.assertFalse(_usuario_tem_acesso_ticket(self.diego, self.t_ana))
        # e ninguém acessa o ticket de diego via location vazia
        self.assertFalse(_usuario_tem_acesso_ticket(self.ana, self.t_diego))

    def test_colega_abre_e_posta_no_detalhe(self):
        self.t_ana.maximo_id = "SR999"
        self.t_ana.save()
        self.client.login(email="bruno@pampa.com", password="123")
        url = reverse("tickets:detalhe_ticket", kwargs={"pk": self.t_ana.pk})
        # GET abre (200)
        self.assertEqual(self.client.get(url).status_code, 200)
        # POST cria interação
        with patch("tickets.views.MaximoSenderService.enviar_interacao", return_value=True), \
             patch("tickets.views.NotificationService.notificar_nova_interacao"):
            self.client.post(url, {"mensagem": "ajuda do colega"})
        self.assertTrue(
            TicketInteracao.objects.filter(ticket=self.t_ana, autor=self.bruno).exists()
        )

    def test_meus_tickets_mostra_equipe(self):
        self.client.login(email="ana@pampa.com", password="123")
        resp = self.client.get(reverse("tickets:meus_tickets"))
        ids = {t.pk for t in resp.context["tickets"]}
        self.assertIn(self.t_ana.pk, ids)
        self.assertIn(self.t_bruno.pk, ids)
        self.assertNotIn(self.t_carla.pk, ids)

    def test_meus_tickets_escopo_meus(self):
        self.client.login(email="ana@pampa.com", password="123")
        resp = self.client.get(reverse("tickets:meus_tickets"), {"escopo": "meus"})
        ids = {t.pk for t in resp.context["tickets"]}
        self.assertEqual(ids, {self.t_ana.pk})

    def test_meus_tickets_escopo_equipe(self):
        self.client.login(email="ana@pampa.com", password="123")
        resp = self.client.get(reverse("tickets:meus_tickets"), {"escopo": "equipe"})
        ids = {t.pk for t in resp.context["tickets"]}
        self.assertEqual(ids, {self.t_bruno.pk})

    def test_meus_tickets_contadores(self):
        self.client.login(email="ana@pampa.com", password="123")
        resp = self.client.get(reverse("tickets:meus_tickets"))
        self.assertEqual(resp.context["count_todos"], 2)
        self.assertEqual(resp.context["count_meus"], 1)
        self.assertEqual(resp.context["count_equipe"], 1)

    def test_pagina_inicial_conta_equipe(self):
        self.client.login(email="ana@pampa.com", password="123")
        resp = self.client.get(reverse("tickets:pagina_inicial"))
        # ana + bruno = 2 abertos (status NEW por padrão), carla não conta
        self.assertEqual(resp.context["total_geral"], 2)
        self.assertEqual(resp.context["total_abertos"], 2)


class SeparacaoGmailTests(TestCase):
    """Contas @gmail são separadas dos clientes corporativos na visão de equipe.

    Dentro da mesma location coexistem 2 mundos: corporativo (domínio próprio)
    e genérico (@gmail). Cada um só enxerga o seu próprio grupo.
    """

    def setUp(self):
        self.corp1 = Cliente.objects.create_user(
            email="ana@pampa.com", username="corp1", password="123", location="PAMPA"
        )
        self.corp2 = Cliente.objects.create_user(
            email="bruno@pampa.com", username="corp2", password="123", location="PAMPA"
        )
        self.gm1 = Cliente.objects.create_user(
            email="pampa@gmail.com", username="gm1", password="123", location="PAMPA"
        )
        self.gm2 = Cliente.objects.create_user(
            email="teste@gmail.com", username="gm2", password="123", location="PAMPA"
        )
        for u in (self.corp1, self.corp2, self.gm1, self.gm2):
            u.precisa_trocar_senha = False
            u.save()
        self.t_corp1 = Ticket.objects.create(cliente=self.corp1, sumario="c1", descricao="d")
        self.t_corp2 = Ticket.objects.create(cliente=self.corp2, sumario="c2", descricao="d")
        self.t_gm1 = Ticket.objects.create(cliente=self.gm1, sumario="g1", descricao="d")
        self.t_gm2 = Ticket.objects.create(cliente=self.gm2, sumario="g2", descricao="d")

    def test_corporativo_nao_ve_gmail(self):
        vis = _tickets_visiveis_cliente(self.corp1)
        self.assertIn(self.t_corp1, vis)
        self.assertIn(self.t_corp2, vis)
        self.assertNotIn(self.t_gm1, vis)
        self.assertNotIn(self.t_gm2, vis)

    def test_gmail_ve_so_gmail_mesma_location(self):
        vis = _tickets_visiveis_cliente(self.gm1)
        self.assertIn(self.t_gm1, vis)
        self.assertIn(self.t_gm2, vis)
        self.assertNotIn(self.t_corp1, vis)
        self.assertNotIn(self.t_corp2, vis)

    def test_permissao_cruzada_gmail_corp_negada(self):
        self.assertFalse(_usuario_tem_acesso_ticket(self.corp1, self.t_gm1))
        self.assertFalse(_usuario_tem_acesso_ticket(self.gm1, self.t_corp1))

    def test_permissao_gmail_entre_gmail_concede(self):
        self.assertTrue(_usuario_tem_acesso_ticket(self.gm1, self.t_gm2))

    def test_permissao_corp_entre_corp_concede(self):
        self.assertTrue(_usuario_tem_acesso_ticket(self.corp1, self.t_corp2))


class FiltroMultiStatusTests(TestCase):
    """Filtro de status com múltipla seleção em meus_tickets e fila_atendimento."""

    def setUp(self):
        self.client = Client()
        self.user = Cliente.objects.create_user(
            email="u@acme.com", username="u", password="123", location="ACME"
        )
        self.user.precisa_trocar_senha = False
        self.user.save()
        self.t_new = Ticket.objects.create(
            cliente=self.user, sumario="n", descricao="d", status_maximo="NEW"
        )
        self.t_prog = Ticket.objects.create(
            cliente=self.user, sumario="p", descricao="d", status_maximo="INPROG"
        )
        self.t_closed = Ticket.objects.create(
            cliente=self.user, sumario="c", descricao="d", status_maximo="CLOSED"
        )

    def test_meus_tickets_multi_status(self):
        self.client.login(email="u@acme.com", password="123")
        resp = self.client.get(
            reverse("tickets:meus_tickets"), {"status": ["NEW", "INPROG"]}
        )
        ids = {t.pk for t in resp.context["tickets"]}
        self.assertEqual(ids, {self.t_new.pk, self.t_prog.pk})
        self.assertEqual(resp.context["status_selecionados"], ["NEW", "INPROG"])

    def test_meus_tickets_status_unico_ainda_funciona(self):
        self.client.login(email="u@acme.com", password="123")
        resp = self.client.get(reverse("tickets:meus_tickets"), {"status": "CLOSED"})
        ids = {t.pk for t in resp.context["tickets"]}
        self.assertEqual(ids, {self.t_closed.pk})

    def test_fila_multi_status(self):
        staff = Cliente.objects.create_user(
            email="s@acme.com", username="s", password="123", is_staff=True
        )
        staff.precisa_trocar_senha = False
        staff.save()
        # fila exclui tickets sem maximo_id
        for t in (self.t_new, self.t_prog, self.t_closed):
            t.maximo_id = f"SR{t.pk}"
            t.save()
        self.client.login(email="s@acme.com", password="123")
        resp = self.client.get(
            reverse("tickets:fila_atendimento"), {"status": ["NEW", "CLOSED"]}
        )
        ids = {t.pk for t in resp.context["tickets"]}
        self.assertEqual(ids, {self.t_new.pk, self.t_closed.pk})
