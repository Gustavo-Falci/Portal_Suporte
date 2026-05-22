from django.contrib.auth.models import Group
from django.test import TestCase


class IoTGroupsMigrationTest(TestCase):
    def test_grupos_iot_existem_apos_migrate(self):
        self.assertTrue(Group.objects.filter(name="IoT_Cliente").exists())
        self.assertTrue(Group.objects.filter(name="IoT_Suporte").exists())
