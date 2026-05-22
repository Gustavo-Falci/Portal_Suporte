import json

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse

from tickets.models import Equipamento, Local

Cliente = get_user_model()


class ApiEquipamentosPorLocalTest(TestCase):
    def setUp(self):
        self.client_http = Client()
        self.user = Cliente.objects.create_user(
            username="iot@test.com", email="iot@test.com", password="x"
        )
        grupo_iot, _ = Group.objects.get_or_create(name="IoT_Cliente")
        self.user.groups.add(grupo_iot)

        self.outro_user = Cliente.objects.create_user(
            username="outro@test.com", email="outro@test.com", password="x"
        )

        self.local_meu = Local.objects.create(nome_local="Meu Local", numero_ativo="L-MEU")
        self.local_meu.clientes.add(self.user)

        self.local_outro = Local.objects.create(nome_local="Outro", numero_ativo="L-OUTRO")
        self.local_outro.clientes.add(self.outro_user)

        self.eq_meu = Equipamento.objects.create(
            local=self.local_meu, nome_equipamento="Sensor", numero_ativo="EQ-MEU"
        )
        self.eq_outro = Equipamento.objects.create(
            local=self.local_outro, nome_equipamento="Sensor X", numero_ativo="EQ-OUTRO"
        )

    def _login(self):
        self.client_http.force_login(self.user)

    def test_requer_login(self):
        url = reverse("tickets:api_equipamentos_por_local") + f"?local_id={self.local_meu.id}"
        resp = self.client_http.get(url)
        self.assertEqual(resp.status_code, 302)

    def test_lista_equipamentos_do_meu_local(self):
        self._login()
        url = reverse("tickets:api_equipamentos_por_local") + f"?local_id={self.local_meu.id}"
        resp = self.client_http.get(url)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(len(data["equipamentos"]), 1)
        eq = data["equipamentos"][0]
        self.assertEqual(eq["id"], self.eq_meu.id)
        self.assertEqual(eq["label"], "Sensor (EQ-MEU)")

    def test_post_nao_permitido(self):
        self._login()
        url = reverse("tickets:api_equipamentos_por_local")
        resp = self.client_http.post(url, {"local_id": self.local_meu.id})
        self.assertEqual(resp.status_code, 405)

    def test_idor_bloqueado_local_de_outro_user(self):
        self._login()
        url = reverse("tickets:api_equipamentos_por_local") + f"?local_id={self.local_outro.id}"
        resp = self.client_http.get(url)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["equipamentos"], [])

    def test_local_id_invalido_retorna_vazio(self):
        self._login()
        url = reverse("tickets:api_equipamentos_por_local") + "?local_id=abc"
        resp = self.client_http.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.content)["equipamentos"], [])

    def test_sem_local_id_retorna_vazio(self):
        self._login()
        url = reverse("tickets:api_equipamentos_por_local")
        resp = self.client_http.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.content)["equipamentos"], [])


from tickets.views import _usuario_tem_acesso_ticket
from tickets.models import Ticket


class UsuarioTemAcessoTicketIoTTest(TestCase):
    def setUp(self):
        self.cliente_iot = Cliente.objects.create_user(
            username="ciot@test.com", email="ciot@test.com", password="x"
        )
        Group.objects.get_or_create(name="IoT_Cliente")
        Group.objects.get_or_create(name="IoT_Suporte")
        self.cliente_iot.groups.add(Group.objects.get(name="IoT_Cliente"))

        self.suporte_iot = Cliente.objects.create_user(
            username="siot@test.com", email="siot@test.com", password="x"
        )
        self.suporte_iot.groups.add(Group.objects.get(name="IoT_Suporte"))

        self.cliente_ti = Cliente.objects.create_user(
            username="cti@test.com", email="cti@test.com", password="x"
        )

        self.ticket_iot = Ticket.objects.create(
            cliente=self.cliente_iot, sumario="x", descricao="y", prioridade="3"
        )
        self.ticket_ti = Ticket.objects.create(
            cliente=self.cliente_ti, sumario="x", descricao="y", prioridade="3"
        )

    def test_iot_suporte_acessa_ticket_de_iot_cliente(self):
        self.assertTrue(_usuario_tem_acesso_ticket(self.suporte_iot, self.ticket_iot))

    def test_iot_suporte_nao_acessa_ticket_de_cliente_ti(self):
        self.assertFalse(_usuario_tem_acesso_ticket(self.suporte_iot, self.ticket_ti))

    def test_cliente_iot_acessa_proprio_ticket(self):
        self.assertTrue(_usuario_tem_acesso_ticket(self.cliente_iot, self.ticket_iot))


class CriarTicketContextoIsIotTest(TestCase):
    def setUp(self):
        self.client_http = Client()
        self.user_iot = Cliente.objects.create_user(
            username="iot@test.com", email="iot@test.com", password="x"
        )
        grupo, _ = Group.objects.get_or_create(name="IoT_Cliente")
        self.user_iot.groups.add(grupo)

        self.user_ti = Cliente.objects.create_user(
            username="ti@test.com", email="ti@test.com", password="x"
        )

    def test_is_iot_true_no_contexto_para_iot_cliente(self):
        self.client_http.force_login(self.user_iot)
        resp = self.client_http.get(reverse("tickets:criar_ticket"))
        self.assertTrue(resp.context["is_iot"])

    def test_is_iot_false_no_contexto_para_user_ti(self):
        self.client_http.force_login(self.user_ti)
        resp = self.client_http.get(reverse("tickets:criar_ticket"))
        self.assertFalse(resp.context["is_iot"])


class FilaAtendimentoIoTSuporteTest(TestCase):
    def setUp(self):
        self.client_http = Client()
        Group.objects.get_or_create(name="IoT_Cliente")
        Group.objects.get_or_create(name="IoT_Suporte")

        self.suporte_iot = Cliente.objects.create_user(
            username="siot@test.com", email="siot@test.com", password="x"
        )
        self.suporte_iot.groups.add(Group.objects.get(name="IoT_Suporte"))

        self.cliente_iot = Cliente.objects.create_user(
            username="ciot@test.com", email="ciot@test.com", password="x"
        )
        self.cliente_iot.groups.add(Group.objects.get(name="IoT_Cliente"))

        self.cliente_ti = Cliente.objects.create_user(
            username="cti@test.com", email="cti@test.com", password="x"
        )

        self.ticket_iot = Ticket.objects.create(
            cliente=self.cliente_iot, sumario="iot tkt", descricao="x",
            prioridade="3", maximo_id="SR-1",
        )
        self.ticket_ti = Ticket.objects.create(
            cliente=self.cliente_ti, sumario="ti tkt", descricao="x",
            prioridade="3", maximo_id="SR-2",
        )

    def test_iot_suporte_ve_apenas_tickets_iot_na_fila(self):
        self.client_http.force_login(self.suporte_iot)
        resp = self.client_http.get(reverse("tickets:fila_atendimento"))
        self.assertEqual(resp.status_code, 200)
        tickets_na_pagina = list(resp.context["tickets"])
        self.assertIn(self.ticket_iot, tickets_na_pagina)
        self.assertNotIn(self.ticket_ti, tickets_na_pagina)

    def test_iot_suporte_ve_apenas_tickets_iot_na_home(self):
        self.client_http.force_login(self.suporte_iot)
        resp = self.client_http.get(reverse("tickets:pagina_inicial"))
        self.assertEqual(resp.status_code, 200)
        ultimos = list(resp.context["ultimos_tickets"])
        self.assertIn(self.ticket_iot, ultimos)
        self.assertNotIn(self.ticket_ti, ultimos)
