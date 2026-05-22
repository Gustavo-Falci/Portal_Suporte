from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase

from tickets.forms import TicketForm
from tickets.models import Ambiente, Equipamento, Local

Cliente = get_user_model()


class TicketFormIoTModeTest(TestCase):
    def setUp(self):
        self.iot_cliente = Cliente.objects.create_user(
            username="iot@test.com", email="iot@test.com", password="x"
        )
        grupo_iot, _ = Group.objects.get_or_create(name="IoT_Cliente")
        self.iot_cliente.groups.add(grupo_iot)

        self.local_a = Local.objects.create(nome_local="Fábrica SP", numero_ativo="LOC-A")
        self.local_a.clientes.add(self.iot_cliente)
        self.local_b = Local.objects.create(nome_local="Fábrica RJ", numero_ativo="LOC-B")

        self.eq_a = Equipamento.objects.create(
            local=self.local_a, nome_equipamento="Sensor", numero_ativo="EQ-A"
        )
        self.eq_b = Equipamento.objects.create(
            local=self.local_b, nome_equipamento="Sensor Outro", numero_ativo="EQ-B"
        )

    def test_form_iot_lista_apenas_locais_do_user(self):
        form = TicketForm(user=self.iot_cliente)
        locais_qs = form.fields["local"].queryset
        self.assertIn(self.local_a, locais_qs)
        self.assertNotIn(self.local_b, locais_qs)

    def test_form_iot_lista_apenas_equipamentos_dos_locais_do_user(self):
        form = TicketForm(user=self.iot_cliente)
        eqs_qs = form.fields["equipamento"].queryset
        self.assertIn(self.eq_a, eqs_qs)
        self.assertNotIn(self.eq_b, eqs_qs)

    def test_form_iot_local_obrigatorio(self):
        form = TicketForm(user=self.iot_cliente)
        self.assertTrue(form.fields["local"].required)

    def test_form_iot_equipamento_obrigatorio(self):
        form = TicketForm(user=self.iot_cliente)
        self.assertTrue(form.fields["equipamento"].required)

    def test_form_iot_ambiente_e_area_ocultos(self):
        from django.forms import HiddenInput
        form = TicketForm(user=self.iot_cliente)
        self.assertIsInstance(form.fields["ambiente"].widget, HiddenInput)
        self.assertIsInstance(form.fields["area"].widget, HiddenInput)

    def test_form_iot_valida_equipamento_pertence_ao_local(self):
        form = TicketForm(
            data={
                "sumario": "x", "descricao": "y", "prioridade": "3",
                "local": self.local_a.id, "equipamento": self.eq_b.id,
            },
            user=self.iot_cliente,
        )
        # eq_b normalmente não passaria do queryset (não pertence aos locais do user)
        # liberamos o queryset para testar exclusivamente o clean cruzado
        form.fields["equipamento"].queryset = Equipamento.objects.all()
        self.assertFalse(form.is_valid())
        self.assertIn("equipamento", form.errors)


class TicketFormTIModeTest(TestCase):
    """Regressão: usuário sem grupo IoT continua no fluxo TI."""

    def setUp(self):
        self.ti_user = Cliente.objects.create_user(
            username="ti@test.com", email="ti@test.com", password="x"
        )
        self.amb = Ambiente.objects.create(nome_ambiente="A", numero_ativo="ATV-1")
        self.amb.clientes.add(self.ti_user)

    def test_form_ti_lista_ambientes(self):
        form = TicketForm(user=self.ti_user)
        self.assertIn(self.amb, form.fields["ambiente"].queryset)

    def test_form_ti_local_e_equipamento_ocultos(self):
        from django.forms import HiddenInput
        form = TicketForm(user=self.ti_user)
        self.assertIsInstance(form.fields["local"].widget, HiddenInput)
        self.assertIsInstance(form.fields["equipamento"].widget, HiddenInput)
