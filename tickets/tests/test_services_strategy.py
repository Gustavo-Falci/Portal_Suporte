from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase

from tickets.models import Ambiente, Equipamento, Local, Ticket
from tickets.services import (
    IoTMaximoStrategy,
    MaximoEmailService,
    NotificationService,
    TIMaximoStrategy,
)
from tickets.models import Notificacao, TicketInteracao

Cliente = get_user_model()


class StrategyResolutionTest(TestCase):
    def setUp(self):
        self.user_ti = Cliente.objects.create_user(
            username="ti@test.com", email="ti@test.com", password="x"
        )
        self.user_iot = Cliente.objects.create_user(
            username="iot@test.com", email="iot@test.com", password="x"
        )
        grupo_iot, _ = Group.objects.get_or_create(name="IoT_Cliente")
        self.user_iot.groups.add(grupo_iot)

    def test_user_ti_resolve_para_TIMaximoStrategy(self):
        s = MaximoEmailService._get_strategy(self.user_ti)
        self.assertIsInstance(s, TIMaximoStrategy)

    def test_user_iot_resolve_para_IoTMaximoStrategy(self):
        s = MaximoEmailService._get_strategy(self.user_iot)
        self.assertIsInstance(s, IoTMaximoStrategy)


class TIStrategyPayloadTest(TestCase):
    def setUp(self):
        self.user = Cliente.objects.create_user(
            username="ti@test.com", email="ti@test.com",
            password="x", location="SP-CENTRO", person_id="P001",
        )
        self.ambiente = Ambiente.objects.create(
            nome_ambiente="Sistema A", numero_ativo="ATV-001"
        )
        self.ambiente.clientes.add(self.user)
        self.ticket = Ticket.objects.create(
            cliente=self.user,
            ambiente=self.ambiente,
            sumario="Falha login",
            descricao="Não consigo logar",
            prioridade="3",
        )

    def test_corpo_contem_siteid_itcbr(self):
        corpo = TIMaximoStrategy().gerar_corpo(self.ticket, self.user)
        self.assertIn("SR#SITEID=ITCBR", corpo)
        self.assertNotIn("SR#SITEID=ITCIOT", corpo)

    def test_corpo_usa_ambiente_como_assetnum(self):
        corpo = TIMaximoStrategy().gerar_corpo(self.ticket, self.user)
        self.assertIn("SR#ASSETNUM=ATV-001", corpo)

    def test_corpo_usa_user_location(self):
        corpo = TIMaximoStrategy().gerar_corpo(self.ticket, self.user)
        self.assertIn("SR#LOCATION=SP-CENTRO", corpo)


class IoTStrategyPayloadTest(TestCase):
    def setUp(self):
        self.user = Cliente.objects.create_user(
            username="iot@test.com", email="iot@test.com",
            password="x", person_id="P-IOT-001",
        )
        grupo_iot, _ = Group.objects.get_or_create(name="IoT_Cliente")
        self.user.groups.add(grupo_iot)

        self.local = Local.objects.create(nome_local="Fábrica SP")
        self.local.clientes.add(self.user)
        self.equipamento = Equipamento.objects.create(
            local=self.local, nome_equipamento="Sensor T01", numero_ativo="EQ-500"
        )
        self.ticket = Ticket.objects.create(
            cliente=self.user,
            local=self.local,
            equipamento=self.equipamento,
            sumario="Sensor offline",
            descricao="Sem dados há 2h",
            prioridade="2",
        )

    def test_corpo_contem_siteid_itciot(self):
        corpo = IoTMaximoStrategy().gerar_corpo(self.ticket, self.user)
        self.assertIn("SR#SITEID=ITCIOT", corpo)
        self.assertNotIn("SR#SITEID=ITCBR", corpo)

    def test_corpo_usa_equipamento_como_assetnum(self):
        corpo = IoTMaximoStrategy().gerar_corpo(self.ticket, self.user)
        self.assertIn("SR#ASSETNUM=EQ-500", corpo)

    def test_corpo_usa_local_como_location(self):
        corpo = IoTMaximoStrategy().gerar_corpo(self.ticket, self.user)
        self.assertIn("SR#LOCATION=Fábrica SP", corpo)

    def test_corpo_nao_inclui_itc_area(self):
        corpo = IoTMaximoStrategy().gerar_corpo(self.ticket, self.user)
        self.assertNotIn("SR#ITC_AREA", corpo)

    def test_assunto_inclui_iot(self):
        assunto = IoTMaximoStrategy().assunto(self.ticket)
        self.assertIn("IoT", assunto)


class NotificarNovaInteracaoIoTTest(TestCase):
    def setUp(self):
        grupo_cli, _ = Group.objects.get_or_create(name="IoT_Cliente")
        grupo_sup, _ = Group.objects.get_or_create(name="IoT_Suporte")
        Group.objects.get_or_create(name="lider_suporte")

        self.cliente_iot = Cliente.objects.create_user(
            username="ciot@test.com", email="ciot@test.com", password="x"
        )
        self.cliente_iot.groups.add(grupo_cli)

        self.suporte_iot = Cliente.objects.create_user(
            username="siot@test.com", email="siot@test.com", password="x"
        )
        self.suporte_iot.groups.add(grupo_sup)

        self.outro_user = Cliente.objects.create_user(
            username="outro@test.com", email="outro@test.com", password="x"
        )

        self.ticket = Ticket.objects.create(
            cliente=self.cliente_iot, sumario="x", descricao="y", prioridade="3"
        )
        self.interacao = TicketInteracao.objects.create(
            ticket=self.ticket, autor=self.outro_user, mensagem="oi"
        )

    @patch("tickets.services.NotificationService._enviar_email_generico")
    def test_iot_suporte_recebe_notificacao_em_ticket_iot(self, _mock):
        NotificationService.notificar_nova_interacao(self.ticket, self.interacao)
        notifs_suporte = Notificacao.objects.filter(destinatario=self.suporte_iot)
        self.assertTrue(notifs_suporte.exists())


class NotificarMudancaStatusIoTTest(TestCase):
    def setUp(self):
        grupo_cli, _ = Group.objects.get_or_create(name="IoT_Cliente")
        grupo_sup, _ = Group.objects.get_or_create(name="IoT_Suporte")

        self.cliente_iot = Cliente.objects.create_user(
            username="ciot2@test.com", email="ciot2@test.com", password="x"
        )
        self.cliente_iot.groups.add(grupo_cli)

        self.suporte_iot = Cliente.objects.create_user(
            username="siot2@test.com", email="siot2@test.com", password="x"
        )
        self.suporte_iot.groups.add(grupo_sup)

        self.ticket = Ticket.objects.create(
            cliente=self.cliente_iot, sumario="x", descricao="y",
            prioridade="3", status_maximo="INPROG",
        )

    @patch("tickets.services.NotificationService._enviar_email_generico")
    def test_iot_suporte_recebe_notificacao_mudanca_status(self, _mock):
        NotificationService.notificar_mudanca_status(self.ticket, "Novo")
        notifs = Notificacao.objects.filter(destinatario=self.suporte_iot, tipo="status")
        self.assertTrue(notifs.exists())
