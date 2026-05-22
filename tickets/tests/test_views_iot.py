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
        self.assertEqual(data["equipamentos"][0]["id"], self.eq_meu.id)

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
