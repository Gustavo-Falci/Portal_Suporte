from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase

from tickets.models import Ambiente, Equipamento, Local, Ticket
from tickets.services import (
    IoTMaximoStrategy,
    MaximoEmailService,
    TIMaximoStrategy,
)

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

        self.local = Local.objects.create(
            nome_local="Fábrica SP", numero_ativo="LOC-100"
        )
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
        self.assertIn("SR#LOCATION=LOC-100", corpo)

    def test_corpo_nao_inclui_itc_area(self):
        corpo = IoTMaximoStrategy().gerar_corpo(self.ticket, self.user)
        self.assertNotIn("SR#ITC_AREA", corpo)

    def test_assunto_inclui_iot(self):
        assunto = IoTMaximoStrategy().assunto(self.ticket)
        self.assertIn("IoT", assunto)
