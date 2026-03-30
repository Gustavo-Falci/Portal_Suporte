import os
import logging
from storages.backends.s3boto3 import S3Boto3Storage
from django.core.files.storage import FileSystemStorage

logger = logging.getLogger(__name__)

class ToleranteS3Storage(S3Boto3Storage):
    """
    Storage customizado com Alta Disponibilidade (Self-Healing).
    Protege o ciclo completo (verificação e upload) contra falhas de rede/S3.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.local_storage = FileSystemStorage()

    def save(self, name, content, max_length=None):
        """
        Sobrescrevemos o método 'save' principal. 
        Isso impede que checagens prévias (como o exists() do Boto3) 
        quebrem a aplicação antes mesmo do upload começar.
        """

        print(f"\n🚀 [ToleranteS3Storage] Interceptando upload do arquivo: {name}")
        
        try:
            # TENTA O FLUXO COMPLETO NA NUVEM
            return super().save(name, content, max_length=max_length)
        except Exception as e:
            print(f"⚠️ [ToleranteS3Storage] NUVEM FORA DO AR! O erro foi: {e}")
            print(f"🛡️ [ToleranteS3Storage] Salvando no disco local de emergência...")

            logger.critical(f"Falha de nuvem ao salvar '{name}': {e}. Acionando disco local.")
            
            # Reposiciona o ponteiro de leitura do arquivo para o início (obrigatório)
            if hasattr(content, 'seek'):
                content.seek(0)
            
            # Salva silenciosamente no disco da Máquina Virtual
            return self.local_storage.save(name, content, max_length=max_length)

    def is_local(self, name) -> bool:
        """Helper para saber de onde servir o arquivo"""
        return self.local_storage.exists(name)

    def open(self, name, mode='rb'):
        # Abre do disco se estiver lá, senão vai buscar na nuvem
        if self.is_local(name):
            return self.local_storage.open(name, mode)
        return super().open(name, mode)
        
    def url(self, name):
        """Garante que o Django Admin ou templates não quebrem ao pedir a URL"""
        if self.is_local(name):
            return self.local_storage.url(name)
        return super().url(name)