import logging
from django.test import TestCase, Client, SimpleTestCase, RequestFactory
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.urls import reverse
from unittest.mock import patch
from .models import Ticket, TicketInteracao, Ambiente, Notificacao, Area
from .forms import TicketForm
from .services import MaximoSenderService, NotificationService
from tickets.views import _tickets_visiveis_cliente, _usuario_tem_acesso_ticket
from tickets.middleware import RequestLogMiddleware
from tickets import audit


class AuditHelperTest(SimpleTestCase):
    def test_registrar_formata_usuario_e_acao(self):
        class FakeUser:
            username = "gu.falci"
        with self.assertLogs("portal.audit", level="INFO") as cm:
            audit.registrar(FakeUser(), "criou Ticket #1234")
        self.assertIn("user=gu.falci criou Ticket #1234", cm.output[0])

    def test_registrar_usuario_anonimo(self):
        with self.assertLogs("portal.audit", level="INFO") as cm:
            audit.registrar(None, "acao qualquer")
        self.assertIn("user=anon acao qualquer", cm.output[0])


class RequestLogMiddlewareTest(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _run(self, request):
        mw = RequestLogMiddleware(lambda req: __import__("django.http", fromlist=["HttpResponse"]).HttpResponse(status=200))
        return mw(request)

    def test_loga_request_normal(self):
        request = self.factory.get("/meus-tickets/")
        request.user = AnonymousUser()
        with self.assertLogs("portal.http", level="INFO") as cm:
            self._run(request)
        linha = cm.output[0]
        self.assertIn("GET", linha)
        self.assertIn("/meus-tickets/", linha)
        self.assertIn("anon", linha)
        self.assertIn("200", linha)

    def test_ignora_estaticos(self):
        request = self.factory.get("/static/css/app.css")
        request.user = AnonymousUser()
        with self.assertRaisesMessage(AssertionError, "no logs"):
            with self.assertLogs("portal.http", level="INFO"):
                self._run(request)

    def test_nao_vaza_dados_sensiveis(self):
        request = self.factory.post("/login/", data={"password": "segredo123"})
        request.user = AnonymousUser()
        with self.assertLogs("portal.http", level="INFO") as cm:
            self._run(request)
        self.assertNotIn("segredo123", cm.output[0])
        self.assertNotIn("password", cm.output[0])

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
        self.client.force_login(self.invasor)
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
        self.client.force_login(self.bruno)
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
        self.client.force_login(self.ana)
        resp = self.client.get(reverse("tickets:meus_tickets"))
        ids = {t.pk for t in resp.context["tickets"]}
        self.assertIn(self.t_ana.pk, ids)
        self.assertIn(self.t_bruno.pk, ids)
        self.assertNotIn(self.t_carla.pk, ids)

    def test_meus_tickets_escopo_meus(self):
        self.client.force_login(self.ana)
        resp = self.client.get(reverse("tickets:meus_tickets"), {"escopo": "meus"})
        ids = {t.pk for t in resp.context["tickets"]}
        self.assertEqual(ids, {self.t_ana.pk})

    def test_meus_tickets_escopo_equipe(self):
        self.client.force_login(self.ana)
        resp = self.client.get(reverse("tickets:meus_tickets"), {"escopo": "equipe"})
        ids = {t.pk for t in resp.context["tickets"]}
        self.assertEqual(ids, {self.t_bruno.pk})

    def test_meus_tickets_contadores(self):
        self.client.force_login(self.ana)
        resp = self.client.get(reverse("tickets:meus_tickets"))
        self.assertEqual(resp.context["count_todos"], 2)
        self.assertEqual(resp.context["count_meus"], 1)
        self.assertEqual(resp.context["count_equipe"], 1)

    def test_pagina_inicial_conta_equipe(self):
        self.client.force_login(self.ana)
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
        self.client.force_login(self.user)
        resp = self.client.get(
            reverse("tickets:meus_tickets"), {"status": ["NEW", "INPROG"]}
        )
        ids = {t.pk for t in resp.context["tickets"]}
        self.assertEqual(ids, {self.t_new.pk, self.t_prog.pk})
        self.assertEqual(resp.context["status_selecionados"], ["NEW", "INPROG"])

    def test_meus_tickets_status_unico_ainda_funciona(self):
        self.client.force_login(self.user)
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
        self.client.force_login(staff)
        resp = self.client.get(
            reverse("tickets:fila_atendimento"), {"status": ["NEW", "CLOSED"]}
        )
        ids = {t.pk for t in resp.context["tickets"]}
        self.assertEqual(ids, {self.t_new.pk, self.t_closed.pk})


class MarcarTodasNotificacoesLidasTests(TestCase):
    """Botão 'marcar todas como lidas' no sino de notificações."""

    def setUp(self):
        self.client = Client()
        self.user = Cliente.objects.create_user(
            email="dono@acme.com", username="dono", password="123"
        )
        self.user.precisa_trocar_senha = False
        self.user.save()
        self.outro = Cliente.objects.create_user(
            email="outro@acme.com", username="outro", password="123"
        )
        # 3 não-lidas do user + 1 já lida
        for i in range(3):
            Notificacao.objects.create(
                destinatario=self.user, mensagem=f"msg {i}", lida=False
            )
        Notificacao.objects.create(
            destinatario=self.user, mensagem="ja lida", lida=True
        )
        # notificação não-lida de outro usuário (não deve ser tocada)
        self.notif_outro = Notificacao.objects.create(
            destinatario=self.outro, mensagem="do outro", lida=False
        )

    def test_marca_todas_nao_lidas_do_usuario(self):
        self.client.force_login(self.user)
        resp = self.client.post(reverse("tickets:marcar_todas_notificacoes_lidas"))
        self.assertEqual(resp.status_code, 302)
        nao_lidas = Notificacao.objects.filter(
            destinatario=self.user, lida=False
        ).count()
        self.assertEqual(nao_lidas, 0)

    def test_nao_toca_notificacoes_de_outro_usuario(self):
        self.client.force_login(self.user)
        self.client.post(reverse("tickets:marcar_todas_notificacoes_lidas"))
        self.notif_outro.refresh_from_db()
        self.assertFalse(self.notif_outro.lida)

    def test_get_nao_permitido(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("tickets:marcar_todas_notificacoes_lidas"))
        self.assertEqual(resp.status_code, 405)


class AreaMultiplosClientesTests(TestCase):
    """Area aceita múltiplos clientes (M2M) e o TicketForm filtra por vínculo."""

    def setUp(self):
        # PAMPA está na lista hardcoded empresas_com_area do TicketForm
        self.ana = Cliente.objects.create_user(
            email="ana@pampa.com", username="ana", password="123", location="PAMPA"
        )
        self.bruno = Cliente.objects.create_user(
            email="bruno@pampa.com", username="bruno", password="123", location="PAMPA"
        )
        self.carla = Cliente.objects.create_user(
            email="carla@pampa.com", username="carla", password="123", location="PAMPA"
        )
        self.area = Area.objects.create(nome_area="Financeiro")
        self.area.clientes.add(self.ana, self.bruno)

    def test_dois_clientes_vinculados_veem_mesma_area(self):
        form_ana = TicketForm(user=self.ana)
        form_bruno = TicketForm(user=self.bruno)
        self.assertIn(self.area, form_ana.fields["area"].queryset)
        self.assertIn(self.area, form_bruno.fields["area"].queryset)

    def test_cliente_nao_vinculado_nao_ve_area(self):
        form_carla = TicketForm(user=self.carla)
        self.assertNotIn(self.area, form_carla.fields["area"].queryset)

    def test_related_name_areas_preservado(self):
        self.assertIn(self.area, self.ana.areas.all())


from io import StringIO
from datetime import timedelta
from django.utils import timezone
from django.core.management.base import OutputWrapper
from tickets.management.commands.sincronizar_maximo import Command


class ParseMaximoDateTests(SimpleTestCase):
    def setUp(self):
        self.cmd = Command()

    def test_parseia_iso_com_offset(self):
        dt = self.cmd._parse_maximo_date("2026-06-18T14:30:00-03:00")
        self.assertIsNotNone(dt)
        self.assertTrue(timezone.is_aware(dt))

    def test_torna_aware_data_naive(self):
        dt = self.cmd._parse_maximo_date("2026-06-18T14:30:00")
        self.assertIsNotNone(dt)
        self.assertTrue(timezone.is_aware(dt))

    def test_retorna_none_para_vazio(self):
        self.assertIsNone(self.cmd._parse_maximo_date(""))
        self.assertIsNone(self.cmd._parse_maximo_date(None))

    def test_retorna_none_para_invalido(self):
        self.assertIsNone(self.cmd._parse_maximo_date("xx/yy/zz"))


class SyncMaximoMatchTests(TestCase):
    """Guarda de data no match por texto do sync Maximo."""

    def _cmd(self):
        cmd = Command()
        cmd.stdout = OutputWrapper(StringIO())  # silencia saída no teste
        return cmd

    def _novo_ticket(self, sumario="Sistema Inoperante"):
        user = Cliente.objects.create(email="u@sync.com", username="u_sync")
        return Ticket.objects.create(
            cliente=user, sumario=sumario, descricao="d", status_maximo="NEW"
        )

    def _item(self, ticket, *, ticketid, status, delta):
        """Monta um item da API com reportdate = data_criacao + delta."""
        return {
            "ticketid": ticketid,
            "description": ticket.sumario,
            "status": status,
            "owner": "tecnico",
            "reportdate": (ticket.data_criacao + delta).isoformat(),
        }

    def test_nao_vincula_sr_fechado_antigo(self):
        # Bug original: SR CLOSED antigo no histórico com mesmo nome.
        # Barrado pela guarda de DATA (reportdate < criação), não pelo status.
        ticket = self._novo_ticket()
        item = self._item(ticket, ticketid="SR-OLD", status="CLOSED",
                          delta=timedelta(days=-30))
        self._cmd().processar_tickets([item])
        ticket.refresh_from_db()
        self.assertIsNone(ticket.maximo_id)
        self.assertEqual(ticket.status_maximo, "NEW")

    def test_vincula_sr_fechado_recente(self):
        # SR CLOSED com reportdate recente (ticket aberto+fechado rápido) DEVE
        # vincular e fechar no portal, mesmo sem vínculo prévio.
        ticket = self._novo_ticket()
        item = self._item(ticket, ticketid="SR-FAST", status="CLOSED",
                          delta=timedelta(minutes=1))
        self._cmd().processar_tickets([item])
        ticket.refresh_from_db()
        self.assertEqual(ticket.maximo_id, "SR-FAST")
        self.assertEqual(ticket.status_maximo, "CLOSED")

    def test_nao_vincula_sr_ativo_anterior_a_criacao(self):
        # Isola a guarda de DATA (status não-terminal, mas reportdate antiga)
        ticket = self._novo_ticket()
        item = self._item(ticket, ticketid="SR-OLD2", status="INPROG",
                          delta=timedelta(hours=-2))
        self._cmd().processar_tickets([item])
        ticket.refresh_from_db()
        self.assertIsNone(ticket.maximo_id)
        self.assertEqual(ticket.status_maximo, "NEW")

    def test_nao_vincula_sem_reportdate(self):
        ticket = self._novo_ticket()
        item = {"ticketid": "SR-X", "description": ticket.sumario,
                "status": "INPROG", "owner": "t", "reportdate": ""}
        self._cmd().processar_tickets([item])
        ticket.refresh_from_db()
        self.assertIsNone(ticket.maximo_id)

    def test_vincula_sr_recente(self):
        ticket = self._novo_ticket()
        item = self._item(ticket, ticketid="SR-NEW", status="INPROG",
                          delta=timedelta(minutes=1))
        self._cmd().processar_tickets([item])
        ticket.refresh_from_db()
        self.assertEqual(ticket.maximo_id, "SR-NEW")
        self.assertEqual(ticket.status_maximo, "INPROG")

    def test_vincula_dentro_do_buffer(self):
        # reportdate 2 min ANTES da criação ainda casa (buffer de 5 min)
        ticket = self._novo_ticket()
        item = self._item(ticket, ticketid="SR-BUF", status="INPROG",
                          delta=timedelta(minutes=-2))
        self._cmd().processar_tickets([item])
        ticket.refresh_from_db()
        self.assertEqual(ticket.maximo_id, "SR-BUF")

    def test_ticket_ja_vinculado_fecha_normal(self):
        # Guarda NÃO afeta tickets já vinculados: fechamento legítimo passa
        ticket = self._novo_ticket()
        ticket.maximo_id = "SR-LIGADO"
        ticket.save()
        item = {"ticketid": "SR-LIGADO", "description": ticket.sumario,
                "status": "CLOSED", "owner": "t",
                "reportdate": (ticket.data_criacao - timedelta(days=10)).isoformat()}
        self._cmd().processar_tickets([item])
        ticket.refresh_from_db()
        self.assertEqual(ticket.status_maximo, "CLOSED")


from tickets.management.commands.auditar_vinculos_maximo import Command as AuditCommand


class AuditarVinculosTests(TestCase):
    """Auditoria read-only de vínculos legados errados (SR anterior à criação)."""

    def _cmd(self):
        cmd = AuditCommand()
        cmd._buf = StringIO()
        cmd.stdout = OutputWrapper(cmd._buf)
        return cmd

    def _ticket(self, maximo_id, status="CLOSED", sumario="Sistema Inoperante"):
        user = Cliente.objects.create(email=f"{maximo_id}@a.com", username=f"u{maximo_id}")
        return Ticket.objects.create(
            cliente=user, sumario=sumario, descricao="d",
            status_maximo=status, maximo_id=maximo_id,
        )

    def _item(self, ticket, *, ticketid, status, delta, sumario=None):
        return {
            "ticketid": ticketid,
            "description": sumario if sumario is not None else ticket.sumario,
            "status": status,
            "reportdate": (ticket.data_criacao + delta).isoformat(),
        }

    def test_sinaliza_vinculo_legado_e_sugere_sr_correto(self):
        # Ticket colado num SR antigo CLOSED; existe SR recente de mesmo nome.
        ticket = self._ticket(maximo_id="2177", status="CLOSED")
        itens = [
            self._item(ticket, ticketid="2177", status="CLOSED", delta=timedelta(days=-30)),
            self._item(ticket, ticketid="2260", status="DOC", delta=timedelta(minutes=1)),
        ]
        cmd = self._cmd()
        cmd.auditar(itens)
        out = cmd._buf.getvalue()
        self.assertIn("[SUSPEITO]", out)
        self.assertIn(f"Ticket #{ticket.id}", out)
        self.assertIn("2260", out)                       # sugere o SR correto
        self.assertIn("Vínculos SUSPEITOS (legado): 1", out)

    def test_nao_sinaliza_vinculo_coerente(self):
        # Ticket vinculado a SR cujo reportdate é posterior à criação: OK.
        ticket = self._ticket(maximo_id="2260", status="DOC")
        itens = [
            self._item(ticket, ticketid="2260", status="DOC", delta=timedelta(minutes=1)),
        ]
        cmd = self._cmd()
        cmd.auditar(itens)
        out = cmd._buf.getvalue()
        self.assertNotIn("[SUSPEITO]", out)
        self.assertIn("Nenhum vínculo suspeito encontrado.", out)


import json as _json
from unittest.mock import MagicMock
from django.core.files.uploadedfile import SimpleUploadedFile


class CriarSRTests(TestCase):
    """Criação da SR no Maximo via REST (substitui e-mail Listener)."""

    def setUp(self):
        self.user = Cliente.objects.create(
            email="sr@teste.com", username="sr_user",
            location="PAMPA", person_id="PESSOA01",
        )
        self.ambiente = Ambiente.objects.create(
            nome_ambiente="ERP", numero_ativo="008"
        )
        self.ambiente.clientes.add(self.user)
        self.area = Area.objects.create(nome_area="Financeiro")
        self.ticket = Ticket.objects.create(
            cliente=self.user, sumario="Erro no ERP",
            descricao="Trava ao salvar", prioridade="2",
            ambiente=self.ambiente, area=self.area,
        )

    def _resp(self, status, body):
        m = MagicMock()
        m.status_code = status
        m.json.return_value = body
        m.text = _json.dumps(body)
        return m

    @patch("tickets.services.requests.post")
    def test_sucesso_retorna_record_com_ticketid(self, mock_post):
        mock_post.return_value = self._resp(201, {
            "ticketid": "2277",
            "href": "https://mx/os/ITC_PORTAL_API/_ABC--",
            "doclinks": {"href": "https://mx/os/ITC_PORTAL_API/_ABC--/doclinks"},
        })
        sr = MaximoSenderService.criar_sr(self.ticket, self.user)
        self.assertIsNotNone(sr)
        self.assertEqual(sr["ticketid"], "2277")

    @patch("tickets.services.requests.post")
    def test_monta_payload_completo(self, mock_post):
        mock_post.return_value = self._resp(201, {"ticketid": "1"})
        MaximoSenderService.criar_sr(self.ticket, self.user)
        enviado = _json.loads(mock_post.call_args.kwargs["data"])
        self.assertEqual(enviado["class"], "SR")
        self.assertEqual(enviado["siteid"], "ITCBR")
        self.assertEqual(enviado["description"], "Erro no ERP")
        self.assertEqual(enviado["description_longdescription"], "Trava ao salvar")
        self.assertEqual(enviado["reportedpriority"], 2)  # inteiro, não "2"
        self.assertEqual(enviado["assetnum"], "008")
        self.assertEqual(enviado["itc_area"], "Financeiro")
        self.assertEqual(enviado["location"], "PAMPA")
        self.assertEqual(enviado["affectedpersonid"], "PESSOA01")
        self.assertEqual(enviado["reportedby"], "PESSOA01")

    @patch("tickets.services.requests.post")
    def test_omite_campos_opcionais_vazios(self, mock_post):
        mock_post.return_value = self._resp(201, {"ticketid": "1"})
        user2 = Cliente.objects.create(email="x@x.com", username="x")  # sem location/person_id
        ticket2 = Ticket.objects.create(
            cliente=user2, sumario="s", descricao="d", prioridade="3",
        )  # sem ambiente/area
        MaximoSenderService.criar_sr(ticket2, user2)
        enviado = _json.loads(mock_post.call_args.kwargs["data"])
        for chave in ("assetnum", "itc_area", "location", "affectedpersonid", "reportedby"):
            self.assertNotIn(chave, enviado)

    @patch("tickets.services.requests.post")
    def test_prioridade_invalida_e_omitida(self, mock_post):
        mock_post.return_value = self._resp(201, {"ticketid": "1"})
        self.ticket.prioridade = ""
        self.ticket.save()
        sr = MaximoSenderService.criar_sr(self.ticket, self.user)
        enviado = _json.loads(mock_post.call_args.kwargs["data"])
        self.assertNotIn("reportedpriority", enviado)
        self.assertIsNotNone(sr)

    @patch("tickets.services.requests.post")
    def test_falha_http_retorna_none(self, mock_post):
        mock_post.return_value = self._resp(500, {"Error": "down"})
        self.assertIsNone(MaximoSenderService.criar_sr(self.ticket, self.user))

    @patch("tickets.services.requests.post")
    def test_resposta_sem_ticketid_retorna_none(self, mock_post):
        mock_post.return_value = self._resp(201, {"description": "criou mas sem id"})
        self.assertIsNone(MaximoSenderService.criar_sr(self.ticket, self.user))

    @patch("tickets.services.requests.post", side_effect=Exception("timeout"))
    def test_excecao_retorna_none(self, mock_post):
        self.assertIsNone(MaximoSenderService.criar_sr(self.ticket, self.user))


class DoclinkUploadTests(TestCase):
    """Upload de anexos para os DOCLINKS de uma SR (fluxo de criação REST)."""

    def _arquivo(self, nome="evidencia.png"):
        return SimpleUploadedFile(nome, b"\x89PNG\r\n\x1a\nconteudo", content_type="image/png")

    @patch("tickets.services.requests.post")
    def test_post_doclink_sucesso(self, mock_post):
        mock_post.return_value.status_code = 201
        ok = MaximoSenderService._post_doclink(
            "https://mx/_ABC--/doclinks", self._arquivo(), "KEY"
        )
        self.assertTrue(ok)
        headers = mock_post.call_args.kwargs["headers"]
        self.assertEqual(headers["slug"], "evidencia.png")
        self.assertEqual(headers["apikey"], "KEY")
        self.assertEqual(headers["Content-Type"], "image/png")

    @patch("tickets.services.requests.post")
    def test_post_doclink_falha(self, mock_post):
        mock_post.return_value.status_code = 500
        mock_post.return_value.text = "erro"
        ok = MaximoSenderService._post_doclink(
            "https://mx/_ABC--/doclinks", self._arquivo(), "KEY"
        )
        self.assertFalse(ok)

    @patch("tickets.services.requests.post")
    def test_enviar_anexos_criacao_envia_todos(self, mock_post):
        mock_post.return_value.status_code = 201
        ok = MaximoSenderService.enviar_anexos_criacao(
            "https://mx/_ABC--/doclinks",
            [self._arquivo("a.png"), self._arquivo("b.png")],
        )
        self.assertTrue(ok)
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(mock_post.call_args.kwargs["data"] is not None, True)

    @patch("tickets.services.requests.post")
    def test_enviar_anexos_criacao_lista_vazia_nao_chama_api(self, mock_post):
        ok = MaximoSenderService.enviar_anexos_criacao("https://mx/_ABC--/doclinks", [])
        self.assertTrue(ok)
        mock_post.assert_not_called()

    @patch("tickets.services.requests.post")
    def test_enviar_anexos_criacao_uma_falha_retorna_false(self, mock_post):
        r_ok = MagicMock(); r_ok.status_code = 201
        r_bad = MagicMock(); r_bad.status_code = 500; r_bad.text = "x"
        mock_post.side_effect = [r_ok, r_bad]
        ok = MaximoSenderService.enviar_anexos_criacao(
            "https://mx/_ABC--/doclinks",
            [self._arquivo("a.png"), self._arquivo("b.png")],
        )
        self.assertFalse(ok)


class CriarTicketViewRESTTests(TestCase):
    """View criar_ticket usa REST (criar_sr) e cai no e-mail só em falha."""

    def setUp(self):
        self.client = Client()
        self.user = Cliente.objects.create_user(
            email="abre@acme.com", username="abre", password="123",
            location="ACME", person_id="P01",
        )
        self.user.precisa_trocar_senha = False
        self.user.save()
        self.ambiente = Ambiente.objects.create(nome_ambiente="ERP", numero_ativo="008")
        self.ambiente.clientes.add(self.user)
        self.client.force_login(self.user)

    def _docx(self, nome="req.docx"):
        # Header PK -> categoria zip (docx). MIME .docx está na allowlist do form.
        return SimpleUploadedFile(
            nome, b"PK\x03\x04docxbytes",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    def _post_valido(self):
        data = {
            "sumario": "Erro no ERP",
            "descricao": "Trava ao salvar",
            "prioridade": "2",
            "ambiente": self.ambiente.id,
            "documento_requisicao": self._docx(),
        }
        return self.client.post(reverse("tickets:criar_ticket"), data)

    @patch("tickets.views.MaximoEmailService.enviar_ticket_maximo")
    @patch("tickets.views.MaximoSenderService.enviar_anexos_criacao")
    @patch("tickets.views.MaximoSenderService.criar_sr")
    def test_sucesso_rest_grava_maximo_id_sem_email(self, mock_criar, mock_anexos, mock_email):
        mock_criar.return_value = {
            "ticketid": "2277",
            "href": "https://mx/_ABC--",
            "doclinks": {"href": "https://mx/_ABC--/doclinks"},
        }
        resp = self._post_valido()
        self.assertRedirects(resp, reverse("tickets:ticket_sucesso"))
        ticket = Ticket.objects.get(sumario="Erro no ERP")
        self.assertEqual(ticket.maximo_id, "2277")
        mock_criar.assert_called_once()
        mock_email.assert_not_called()

    @patch("tickets.views.MaximoEmailService.enviar_ticket_maximo")
    @patch("tickets.views.MaximoSenderService.criar_sr", return_value=None)
    def test_falha_rest_cai_no_email(self, mock_criar, mock_email):
        resp = self._post_valido()
        self.assertRedirects(resp, reverse("tickets:ticket_sucesso"))
        ticket = Ticket.objects.get(sumario="Erro no ERP")
        self.assertIsNone(ticket.maximo_id)
        mock_email.assert_called_once()


class CriarTicketLoggingTests(TestCase):
    """Todo evento de criação de ticket deve ser registrado no log
    (sucesso REST, sucesso fallback e-mail, falha REST, erro, form inválido)."""

    def setUp(self):
        self.client = Client()
        self.user = Cliente.objects.create_user(
            email="log@acme.com", username="logger_user", password="123",
            location="ACME", person_id="P01",
        )
        self.user.precisa_trocar_senha = False
        self.user.save()
        self.ambiente = Ambiente.objects.create(nome_ambiente="ERP", numero_ativo="008")
        self.ambiente.clientes.add(self.user)
        self.client.force_login(self.user)

    def _docx(self, nome="req.docx"):
        return SimpleUploadedFile(
            nome, b"PK\x03\x04docxbytes",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    def _post_valido(self):
        data = {
            "sumario": "Erro no ERP",
            "descricao": "Trava ao salvar",
            "prioridade": "2",
            "ambiente": self.ambiente.id,
            "documento_requisicao": self._docx(),
        }
        return self.client.post(reverse("tickets:criar_ticket"), data)

    @patch("tickets.views.MaximoEmailService.enviar_ticket_maximo")
    @patch("tickets.views.MaximoSenderService.enviar_anexos_criacao")
    @patch("tickets.views.MaximoSenderService.criar_sr")
    def test_loga_sucesso_rest(self, mock_criar, mock_anexos, mock_email):
        mock_criar.return_value = {
            "ticketid": "2277",
            "href": "https://mx/_ABC--",
            "doclinks": {"href": "https://mx/_ABC--/doclinks"},
        }
        with self.assertLogs("tickets.views", level="INFO") as cm:
            self._post_valido()
        linhas = "\n".join(cm.output)
        self.assertIn("2277", linhas)
        self.assertRegex(linhas, r"INFO.*[Ss]ucesso|INFO.*criada.*REST|INFO.*via REST")

    @patch("tickets.views.MaximoEmailService.enviar_ticket_maximo")
    @patch("tickets.views.MaximoSenderService.criar_sr", return_value=None)
    def test_loga_falha_rest_e_sucesso_fallback(self, mock_criar, mock_email):
        with self.assertLogs("tickets.views", level="INFO") as cm:
            self._post_valido()
        linhas = "\n".join(cm.output)
        # Falha do REST registrada (WARNING)
        self.assertRegex(linhas, r"WARNING.*(REST|criar_sr).*(falh|fallback)")
        # Sucesso do envio por e-mail registrado (INFO)
        self.assertRegex(linhas, r"INFO.*(e-mail|email|fallback|Listener)")

    @patch("tickets.views.MaximoEmailService.enviar_ticket_maximo", side_effect=Exception("smtp down"))
    @patch("tickets.views.MaximoSenderService.criar_sr", return_value=None)
    def test_loga_erro_fallback_email(self, mock_criar, mock_email):
        with self.assertLogs("tickets.views", level="ERROR") as cm:
            self._post_valido()
        self.assertRegex("\n".join(cm.output), r"ERROR.*fallback")

    def test_loga_form_invalido(self):
        # POST sem documento_requisicao (obrigatório) -> form inválido
        data = {"sumario": "x", "descricao": "y", "prioridade": "2", "ambiente": self.ambiente.id}
        with self.assertLogs("tickets.views", level="WARNING") as cm:
            self.client.post(reverse("tickets:criar_ticket"), data)
        linhas = "\n".join(cm.output)
        self.assertRegex(linhas, r"WARNING.*(inválid|invalid|REJEITAD)")
        # Não deve vazar valores dos campos, só nomes
        self.assertIn("documento_requisicao", linhas)


import tempfile
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage as _FSStorage
from tickets.storage import ToleranteS3Storage


class ToleranteStorageLoggingTests(SimpleTestCase):
    """ToleranteS3Storage usa logger (sem print/emoji) para não quebrar
    stdout cp1252 no Windows e manter rastro no log."""

    def _storage(self):
        # Instancia sem chamar __init__ (evita setup real do S3/boto3).
        st = ToleranteS3Storage.__new__(ToleranteS3Storage)
        st.local_storage = _FSStorage(location=tempfile.mkdtemp())
        return st

    @patch("tickets.storage.S3Boto3Storage.save", return_value="nuvem/arquivo.txt")
    def test_intercept_loga_sem_emoji(self, mock_super_save):
        st = self._storage()
        with self.assertLogs("tickets.storage", level="DEBUG") as cm:
            nome = st.save("arquivo.txt", ContentFile(b"dados"))
        self.assertEqual(nome, "nuvem/arquivo.txt")
        saida = "\n".join(cm.output)
        self.assertIn("arquivo.txt", saida)
        self.assertNotIn("🚀", saida)

    @patch("tickets.storage.S3Boto3Storage.save", side_effect=Exception("nuvem fora"))
    def test_fallback_loga_sem_emoji_e_salva_local(self, mock_super_save):
        st = self._storage()
        with self.assertLogs("tickets.storage", level="WARNING") as cm:
            nome = st.save("local.txt", ContentFile(b"dados"))
        saida = "\n".join(cm.output)
        # Caiu pro disco local e registrou o incidente sem emojis
        self.assertTrue(st.local_storage.exists(nome))
        self.assertNotIn("⚠️", saida)
        self.assertNotIn("🛡️", saida)
        self.assertIn("nuvem fora", saida)


class _SyncThread:
    """Thread falsa que executa o target imediatamente (síncrono) nos testes,
    para checar o efeito do upload de anexos sem corrida de thread real."""

    def __init__(self, target=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class CriarTicketRobustezTests(TestCase):
    """Robustez da criação: falha do Maximo não vira erro de banco (#1) e
    o estado de sincronização de anexos é rastreado (#2)."""

    def setUp(self):
        self.client = Client()
        self.user = Cliente.objects.create_user(
            email="rob@acme.com", username="rob", password="123",
            location="ACME", person_id="P01",
        )
        self.user.precisa_trocar_senha = False
        self.user.save()
        self.ambiente = Ambiente.objects.create(nome_ambiente="ERP", numero_ativo="008")
        self.ambiente.clientes.add(self.user)
        self.client.force_login(self.user)

    def _docx(self, nome="req.docx"):
        return SimpleUploadedFile(
            nome, b"PK\x03\x04docxbytes",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    def _png(self, nome="ev.png"):
        return SimpleUploadedFile(nome, b"\x89PNG\r\n\x1a\nbytes", content_type="image/png")

    def _data(self, **extra):
        data = {
            "sumario": "Erro no ERP",
            "descricao": "Trava ao salvar",
            "prioridade": "2",
            "ambiente": self.ambiente.id,
            "documento_requisicao": self._docx(),
        }
        data.update(extra)
        return data

    def _post(self, **extra):
        return self.client.post(reverse("tickets:criar_ticket"), self._data(**extra))

    # ---------- #1: falha do Maximo não vira erro de banco ----------

    @patch("tickets.views.MaximoSenderService.criar_sr", side_effect=Exception("maximo explodiu"))
    def test_falha_inesperada_maximo_nao_vira_erro_de_banco(self, mock_criar):
        # Erro inesperado na integração NÃO deve re-renderizar o form com
        # "erro ao guardar" (convite a reenvio/duplicado). Ticket fica salvo
        # e o usuário é redirecionado para sucesso.
        resp = self._post()
        self.assertRedirects(resp, reverse("tickets:ticket_sucesso"))
        self.assertTrue(Ticket.objects.filter(sumario="Erro no ERP").exists())

    @patch("tickets.views.TicketAnexo.objects.create", side_effect=Exception("db down"))
    def test_falha_persistencia_nao_cria_ticket(self, mock_create):
        # Falha DENTRO da transação (ao salvar anexo) deve dar rollback total:
        # nenhum ticket persiste, e o form é re-renderizado (status 200).
        resp = self._post(arquivo=self._png())
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Ticket.objects.filter(sumario="Erro no ERP").exists())

    # ---------- #2: rastreio de sincronização de anexos ----------

    @patch("tickets.views.threading.Thread", _SyncThread)
    @patch("tickets.views.MaximoSenderService.enviar_anexos_criacao", return_value=True)
    @patch("tickets.views.MaximoSenderService.criar_sr")
    def test_anexos_sincronizados_true_quando_upload_ok(self, mock_criar, mock_anexos):
        mock_criar.return_value = {
            "ticketid": "2277", "href": "https://mx/_A--",
            "doclinks": {"href": "https://mx/_A--/doclinks"},
        }
        self._post()
        t = Ticket.objects.get(sumario="Erro no ERP")
        self.assertTrue(t.anexos_sincronizados)

    @patch("tickets.views.threading.Thread", _SyncThread)
    @patch("tickets.views.MaximoSenderService.enviar_anexos_criacao", return_value=False)
    @patch("tickets.views.MaximoSenderService.criar_sr")
    def test_anexos_sincronizados_false_quando_upload_falha(self, mock_criar, mock_anexos):
        mock_criar.return_value = {
            "ticketid": "2277", "href": "https://mx/_A--",
            "doclinks": {"href": "https://mx/_A--/doclinks"},
        }
        self._post()
        t = Ticket.objects.get(sumario="Erro no ERP")
        self.assertFalse(t.anexos_sincronizados)

    @patch("tickets.views.MaximoSenderService.enviar_anexos_criacao")
    @patch("tickets.views.MaximoSenderService.criar_sr")
    def test_anexos_sincronizados_false_quando_sr_sem_doclinks(self, mock_criar, mock_anexos):
        # SR criada mas resposta sem doclinks/href -> anexos não podem subir.
        mock_criar.return_value = {"ticketid": "2277"}
        self._post()
        t = Ticket.objects.get(sumario="Erro no ERP")
        self.assertFalse(t.anexos_sincronizados)
        mock_anexos.assert_not_called()


class SeguidoresTests(TestCase):
    """Seguidores: consultores extras designados pela liderança ganham
    acesso de leitura+interação e recebem notificações. Só suporte/líder
    pode designá-los; só usuários do grupo Consultores podem ser seguidores."""

    def setUp(self):
        self.client = Client()
        self.g_consultores = Group.objects.create(name="Consultores")
        self.g_lider = Group.objects.create(name="lider_suporte")

        # Dono corporativo do ticket (location distinta dos consultores).
        self.dono = Cliente.objects.create_user(
            email="dono@corp.com", username="dono", password="123", location="CORP"
        )
        # Líder de suporte (designa seguidores).
        self.lider = Cliente.objects.create_user(
            email="lider@itc.com", username="lider", password="123"
        )
        self.lider.groups.add(self.g_lider)

        # Consultor owner do ticket.
        self.cons_owner = Cliente.objects.create_user(
            email="owner@cons.com", username="cowner", password="123", person_id="P_OWNER"
        )
        self.cons_owner.groups.add(self.g_consultores)

        # Consultor que será seguidor (sem acesso natural ao ticket).
        self.cons_seg = Cliente.objects.create_user(
            email="seg@cons.com", username="cseg", password="123", person_id="P_SEG"
        )
        self.cons_seg.groups.add(self.g_consultores)

        for u in (self.dono, self.lider, self.cons_owner, self.cons_seg):
            u.precisa_trocar_senha = False
            u.save()

        self.ticket = Ticket.objects.create(
            cliente=self.dono, sumario="Privado", descricao="x",
            owner="P_OWNER", maximo_id="SR-1",
        )

    # ---------- Acesso ----------

    def test_consultor_sem_vinculo_nao_acessa(self):
        self.client.force_login(self.cons_seg)
        resp = self.client.get(reverse("tickets:detalhe_ticket", kwargs={"pk": self.ticket.pk}))
        self.assertRedirects(resp, reverse("tickets:meus_tickets"))

    def test_seguidor_ganha_acesso_ao_detalhe(self):
        self.ticket.seguidores.add(self.cons_seg)
        self.client.force_login(self.cons_seg)
        resp = self.client.get(reverse("tickets:detalhe_ticket", kwargs={"pk": self.ticket.pk}))
        self.assertEqual(resp.status_code, 200)

    def test_helper_reconhece_seguidor(self):
        self.assertFalse(_usuario_tem_acesso_ticket(self.cons_seg, self.ticket))
        self.ticket.seguidores.add(self.cons_seg)
        self.assertTrue(_usuario_tem_acesso_ticket(self.cons_seg, self.ticket))

    def test_seguidor_ve_ticket_na_fila(self):
        self.ticket.seguidores.add(self.cons_seg)
        self.client.force_login(self.cons_seg)
        resp = self.client.get(reverse("tickets:fila_atendimento"))
        self.assertContains(resp, "SR-1")

    # ---------- Gerência (quem pode designar) ----------

    def test_lider_define_seguidores(self):
        self.client.force_login(self.lider)
        resp = self.client.post(
            reverse("tickets:gerenciar_seguidores", kwargs={"pk": self.ticket.pk}),
            {"seguidores": [self.cons_seg.id]},
        )
        self.assertRedirects(resp, reverse("tickets:detalhe_ticket", kwargs={"pk": self.ticket.pk}))
        self.assertIn(self.cons_seg, self.ticket.seguidores.all())

    def test_lider_define_seguidores_ajax(self):
        self.client.force_login(self.lider)
        resp = self.client.post(
            reverse("tickets:gerenciar_seguidores", kwargs={"pk": self.ticket.pk}),
            {"seguidores": [self.cons_seg.id]},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "success")
        self.assertIn(self.cons_seg, self.ticket.seguidores.all())

    def test_consultor_nao_pode_designar(self):
        self.client.force_login(self.cons_owner)
        self.client.post(
            reverse("tickets:gerenciar_seguidores", kwargs={"pk": self.ticket.pk}),
            {"seguidores": [self.cons_seg.id]},
        )
        self.assertNotIn(self.cons_seg, self.ticket.seguidores.all())

    def test_apenas_consultores_viram_seguidores(self):
        # dono não é do grupo Consultores -> deve ser ignorado.
        self.client.force_login(self.lider)
        self.client.post(
            reverse("tickets:gerenciar_seguidores", kwargs={"pk": self.ticket.pk}),
            {"seguidores": [self.dono.id, self.cons_seg.id]},
        )
        segs = set(self.ticket.seguidores.all())
        self.assertIn(self.cons_seg, segs)
        self.assertNotIn(self.dono, segs)

    # ---------- Notificação ----------

    @patch.object(NotificationService, "_enviar_email_generico")
    def test_seguidor_recebe_notificacao(self, mock_mail):
        self.ticket.seguidores.add(self.cons_seg)
        interacao = TicketInteracao.objects.create(
            ticket=self.ticket, autor=self.cons_owner, mensagem="atualização"
        )
        NotificationService.notificar_nova_interacao(self.ticket, interacao)
        self.assertTrue(
            Notificacao.objects.filter(destinatario=self.cons_seg, ticket=self.ticket).exists()
        )

    @patch.object(NotificationService, "_enviar_email_generico")
    def test_autor_nao_recebe_notificacao(self, mock_mail):
        self.ticket.seguidores.add(self.cons_seg)
        interacao = TicketInteracao.objects.create(
            ticket=self.ticket, autor=self.cons_seg, mensagem="eu mesmo"
        )
        NotificationService.notificar_nova_interacao(self.ticket, interacao)
        self.assertFalse(
            Notificacao.objects.filter(destinatario=self.cons_seg, ticket=self.ticket).exists()
        )


class TelaSucessoTests(TestCase):
    """Tela de sucesso rica: mostra nº SR, resumo e CTA de acompanhamento,
    com validação de acesso e fallback gracioso sem contexto."""

    def setUp(self):
        self.client = Client()
        self.user = Cliente.objects.create_user(
            email="suc@acme.com", username="suc", password="123",
            location="ACME", person_id="P01",
        )
        self.user.precisa_trocar_senha = False
        self.user.save()
        self.outro = Cliente.objects.create_user(
            email="intruso@acme.com", username="intruso", password="123",
        )
        self.outro.precisa_trocar_senha = False
        self.outro.save()
        self.ambiente = Ambiente.objects.create(nome_ambiente="ERP", numero_ativo="008")
        self.ambiente.clientes.add(self.user)
        self.ticket = Ticket.objects.create(
            cliente=self.user, sumario="Erro no ERP", descricao="Trava ao salvar",
            prioridade="2", ambiente=self.ambiente, maximo_id="2277",
        )

    def _set_session_ticket(self, ticket_id):
        session = self.client.session
        session["ticket_sucesso_id"] = ticket_id
        session.save()

    def test_mostra_numero_sr_e_resumo(self):
        self.client.force_login(self.user)
        self._set_session_ticket(self.ticket.id)
        resp = self.client.get(reverse("tickets:ticket_sucesso"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["ticket"], self.ticket)
        self.assertContains(resp, "2277")          # nº SR
        self.assertContains(resp, "Erro no ERP")   # sumário

    def test_cta_acompanhar_aponta_para_detalhe(self):
        self.client.force_login(self.user)
        self._set_session_ticket(self.ticket.id)
        resp = self.client.get(reverse("tickets:ticket_sucesso"))
        url_detalhe = reverse("tickets:detalhe_ticket", kwargs={"pk": self.ticket.pk})
        self.assertContains(resp, url_detalhe)

    def test_consome_session_uma_vez(self):
        # Após exibir, a chave é removida -> refresh não remostra o ticket.
        self.client.force_login(self.user)
        self._set_session_ticket(self.ticket.id)
        self.client.get(reverse("tickets:ticket_sucesso"))
        resp2 = self.client.get(reverse("tickets:ticket_sucesso"))
        self.assertIsNone(resp2.context["ticket"])

    def test_acesso_direto_sem_session_versao_generica(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("tickets:ticket_sucesso"))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.context["ticket"])

    def test_acl_nega_ticket_de_terceiro(self):
        # intruso (sem location/vínculo) não pode ver resumo do ticket alheio,
        # mesmo que o id esteja na sessão dele.
        self.client.force_login(self.outro)
        self._set_session_ticket(self.ticket.id)
        resp = self.client.get(reverse("tickets:ticket_sucesso"))
        self.assertIsNone(resp.context["ticket"])

    def test_fallback_sem_maximo_id_mostra_em_processamento(self):
        self.ticket.maximo_id = None
        self.ticket.save()
        self.client.force_login(self.user)
        self._set_session_ticket(self.ticket.id)
        resp = self.client.get(reverse("tickets:ticket_sucesso"))
        self.assertEqual(resp.context["ticket"], self.ticket)
        self.assertContains(resp, "processamento")
        # CTA acompanhar ainda presente (usa PK local)
        url_detalhe = reverse("tickets:detalhe_ticket", kwargs={"pk": self.ticket.pk})
        self.assertContains(resp, url_detalhe)

    def test_criar_ticket_grava_id_na_session(self):
        self.client.force_login(self.user)
        with patch("tickets.views.MaximoSenderService.criar_sr", return_value=None), \
             patch("tickets.views.MaximoEmailService.enviar_ticket_maximo"):
            data = {
                "sumario": "Novo problema", "descricao": "detalhe",
                "prioridade": "3", "ambiente": self.ambiente.id,
                "documento_requisicao": SimpleUploadedFile(
                    "req.docx", b"PK\x03\x04docxbytes",
                    content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
            }
            self.client.post(reverse("tickets:criar_ticket"), data)
        novo = Ticket.objects.get(sumario="Novo problema")
        self.assertEqual(self.client.session.get("ticket_sucesso_id"), novo.id)


class ErrosCriacaoTicketTests(TestCase):
    """Feedback de erro no fluxo de criação: erro de banco visível inline,
    form inválido com alerta estático, GET sem erros."""

    def setUp(self):
        self.client = Client()
        self.user = Cliente.objects.create_user(
            email="err@acme.com", username="err", password="123",
            location="ACME", person_id="P01",
        )
        self.user.precisa_trocar_senha = False
        self.user.save()
        self.ambiente = Ambiente.objects.create(nome_ambiente="ERP", numero_ativo="008")
        self.ambiente.clientes.add(self.user)
        self.client.force_login(self.user)

    def _docx(self, nome="req.docx"):
        return SimpleUploadedFile(
            nome, b"PK\x03\x04docxbytes",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    def _png(self, nome="ev.png"):
        return SimpleUploadedFile(nome, b"\x89PNG\r\n\x1a\nbytes", content_type="image/png")

    def _data_valida(self, **extra):
        data = {
            "sumario": "Erro no ERP",
            "descricao": "Trava ao salvar",
            "prioridade": "2",
            "ambiente": self.ambiente.id,
            "documento_requisicao": self._docx(),
        }
        data.update(extra)
        return data

    @patch("tickets.views.TicketAnexo.objects.create", side_effect=Exception("db down"))
    def test_erro_persistencia_mostra_mensagem(self, mock_create):
        # POST válido com evidência -> create() lança -> rollback, re-render 200
        # com a mensagem de erro (antes engolida) visível INLINE no card.
        resp = self.client.post(
            reverse("tickets:criar_ticket"), self._data_valida(arquivo=self._png())
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Ticket.objects.filter(sumario="Erro no ERP").exists())
        self.assertContains(resp, "Ocorreu um erro ao guardar")
        self.assertContains(resp, "telefone ou chat")

    def test_form_invalido_mostra_alerta(self):
        # POST sem documento_requisicao (obrigatório) -> form inválido.
        data = {
            "sumario": "x", "descricao": "y",
            "prioridade": "2", "ambiente": self.ambiente.id,
        }
        resp = self.client.post(reverse("tickets:criar_ticket"), data)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "generalErrorAlert")
        self.assertContains(resp, "corrija os campos destacados")

    def test_get_inicial_sem_erros(self):
        resp = self.client.get(reverse("tickets:criar_ticket"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "corrija os campos destacados")
        self.assertNotContains(resp, "Ocorreu um erro ao guardar")
