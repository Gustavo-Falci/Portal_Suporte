from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase

from tickets.models import Equipamento, Local

Cliente = get_user_model()


class LocalModelTest(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create_user(
            username="iot1@test.com", email="iot1@test.com", password="x"
        )

    def test_cria_local_com_cliente_m2m(self):
        local = Local.objects.create(nome_local="Fábrica SP")
        local.clientes.add(self.cliente)
        self.assertEqual(local.clientes.count(), 1)
        self.assertEqual(self.cliente.locais.count(), 1)

    def test_str_local(self):
        local = Local.objects.create(nome_local="Fábrica SP")
        self.assertEqual(str(local), "Fábrica SP")


class EquipamentoModelTest(TestCase):
    def setUp(self):
        self.local = Local.objects.create(nome_local="Fábrica SP")

    def test_cria_equipamento_com_fk_local(self):
        eq = Equipamento.objects.create(
            local=self.local, nome_equipamento="Sensor T01", numero_ativo="EQ-100"
        )
        self.assertEqual(eq.local, self.local)
        self.assertEqual(self.local.equipamentos.count(), 1)

    def test_str_equipamento(self):
        eq = Equipamento.objects.create(
            local=self.local, nome_equipamento="Sensor T01", numero_ativo="EQ-100"
        )
        self.assertEqual(str(eq), "Sensor T01 (EQ-100)")

    def test_cascade_delete_local_apaga_equipamento(self):
        Equipamento.objects.create(
            local=self.local, nome_equipamento="Sensor T01", numero_ativo="EQ-100"
        )
        self.local.delete()
        self.assertEqual(Equipamento.objects.count(), 0)


class ClienteIoTPropertyTest(TestCase):
    def setUp(self):
        self.cliente = Cliente.objects.create_user(
            username="u@test.com", email="u@test.com", password="x"
        )
        self.grupo_iot_cliente, _ = Group.objects.get_or_create(name="IoT_Cliente")
        self.grupo_iot_suporte, _ = Group.objects.get_or_create(name="IoT_Suporte")

    def test_is_iot_cliente_false_sem_grupo(self):
        self.assertFalse(self.cliente.is_iot_cliente)

    def test_is_iot_cliente_true_com_grupo(self):
        self.cliente.groups.add(self.grupo_iot_cliente)
        self.assertTrue(self.cliente.is_iot_cliente)

    def test_is_iot_suporte_true_com_grupo(self):
        self.cliente.groups.add(self.grupo_iot_suporte)
        self.assertTrue(self.cliente.is_iot_suporte)
