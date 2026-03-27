import os
import boto3
from botocore.config import Config
from django.core.management.base import BaseCommand
from django.conf import settings

class Command(BaseCommand):
    help = 'Sincroniza a pasta media local com o Oracle Cloud Object Storage (OCI)'

    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.SUCCESS("--- INICIANDO MIGRAÇÃO PARA ORACLE CLOUD ---"))

        # --- A CORREÇÃO MÁGICA PARA A ORACLE CLOUD ---
        os.environ['AWS_REQUEST_CHECKSUM_CALCULATION'] = 'when_required'
        os.environ['AWS_RESPONSE_CHECKSUM_VALIDATION'] = 'when_required'
        # ---------------------------------------------

        # Pega as credenciais diretamente do seu .env / variáveis de ambiente
        endpoint = os.getenv("OCI_ENDPOINT_URL")
        bucket = os.getenv("OCI_BUCKET_NAME")
        access_key = os.getenv("OCI_ACCESS_KEY")
        secret_key = os.getenv("OCI_SECRET_KEY")
        region = os.getenv("OCI_REGION_NAME", "sa-saopaulo-1")

        if not all([endpoint, bucket, access_key, secret_key]):
            self.stdout.write(self.style.ERROR("❌ ERRO: Faltam credenciais da Oracle no seu .env!"))
            return

        # Configuração do Boto3 compatível com a OCI
        oracle_config = Config(
            signature_version='s3v4',
            s3={'addressing_style': 'path'}
        )

        try:
            s3 = boto3.client(
                's3',
                region_name=region,
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=oracle_config
            )
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"❌ Erro ao criar cliente de conexão: {e}"))
            return

        # Caminho absoluto da sua pasta media local
        media_root = settings.MEDIA_ROOT
        if not os.path.exists(media_root):
            self.stdout.write(self.style.WARNING(f"⚠️ A pasta media não foi encontrada em: {media_root}"))
            return

        arquivos_enviados = 0
        arquivos_com_erro = 0

        self.stdout.write("Analisando pasta local e iniciando uploads...\n")

        # Percorre todas as subpastas dentro de /media/
        for root, dirs, files in os.walk(media_root):
            for file in files:
                local_path = os.path.join(root, file)
                
                # Descobre o caminho relativo. 
                # Ex: C:\...\media\tickets\2024\arquivo.pdf -> tickets/2024/arquivo.pdf
                relative_path = os.path.relpath(local_path, media_root)
                
                # Garante que as barras no Bucket fiquem como '/' (útil para quem roda no Windows)
                # E adiciona o prefixo 'media/' para manter o padrão que o Django Storages usa
                s3_key = f"media/{relative_path.replace(os.sep, '/')}"

                self.stdout.write(f"Enviando: {relative_path} ... ", ending="")
                
                try:
                    s3.upload_file(
                        Filename=local_path,
                        Bucket=bucket,
                        Key=s3_key
                    )
                    self.stdout.write(self.style.SUCCESS("✅ OK"))
                    arquivos_enviados += 1
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"❌ ERRO: {e}"))
                    arquivos_com_erro += 1

        self.stdout.write(self.style.SUCCESS("\n🎉 MIGRAÇÃO CONCLUÍDA!"))
        self.stdout.write(f"Total de arquivos migrados: {arquivos_enviados}")
        if arquivos_com_erro > 0:
            self.stdout.write(self.style.WARNING(f"Arquivos que falharam: {arquivos_com_erro}"))