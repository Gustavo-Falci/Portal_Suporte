# 🛠️ Portal de Suporte - IT Consol

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![Django](https://img.shields.io/badge/Django-5.x-green.svg)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Ready-blue.svg)
![Bootstrap](https://img.shields.io/badge/Bootstrap-5.3-purple.svg)
![Oracle Cloud](https://img.shields.io/badge/Oracle_Cloud-Ubuntu_VM-red.svg)

Um portal web robusto desenvolvido para clientes realizarem abertura, acompanhamento e interação em chamados (tickets) de suporte corporativo. O sistema atua como um frontend amigável que possui integração direta e automatizada com o **IBM Maximo** através de um Email Listener customizado e rotinas via API REST.

---

## 🚀 Tecnologias e Stack

* **Backend:** Python 3.x, Django 5.x
* **Banco de Dados:** PostgreSQL
* **Frontend:** HTML5, CSS3 Customizado, Bootstrap 5.3
* **Tipografia Visual:** Fonte IBM Plex Mono (Padrão visual corporativo IBM Carbon Design)
* **Infraestrutura:** Oracle Cloud Infrastructure (OCI) - Ubuntu VM (1 vCPU, 6 GB RAM)
* **Servidor Web:** Gunicorn + Nginx

---

## ✨ Principais Funcionalidades e Segurança

* **Autenticação Customizada & Segurança:** Login via E-mail (*Case Insensitive*) utilizando um `ModelBackend` próprio (`EmailBackend`). Inclui mitigação ativa contra *Timing Attacks* na enumeração de usuários.
* **Gestão de Anexos Segura:** Sistema robusto de validação de arquivos (`_validar_anexo_comum`), limitando uploads a 150MB e restrição estrita de *MIME types* e extensões.
* **Comunicação Bidirecional:** Chat interno no ticket (`TicketInteracao`) com upload de evidências e histórico imutável para fins de auditoria.
* **Notificações In-App:** Alertas em tempo real de mudanças de status do ticket (`monitorar_mudancas_ticket` via Signals) e novas mensagens.
* **Arquitetura Baseada em Funções (FBV):** Código padronizado utilizando *Function-Based Views* com forte tipagem (*Type Hints*) no Python.

---

## 💼 Regras de Negócio Críticas

1. **Autenticação via Email:** O usuário (`Cliente`, que estende `AbstractUser`) não utiliza o `username` clássico para login. O acesso é feito estritamente pelo E-mail, que é vinculado ao `person_id` e `location` no IBM Maximo.
2. **Lógica de "Área" Dinâmica:** O campo "Área" (`Area`) no formulário de novos tickets é injetado dinamicamente. Ele só é obrigatório/visível se o usuário pertencer a empresas específicas (ex: "PAMPA" ou "ABL"), verificado através do domínio do e-mail ou vínculo direto na base.

---

## 📂 Arquitetura e Modelos Principais

O sistema está centrado no app principal `tickets`. Os principais modelos de dados (`models.py`) são:

* `Cliente`: Estende `AbstractUser`. Autentica via e-mail e possui campos atrelados ao Maximo (`location`, `person_id`).
* `Ambiente`: Representa os ativos/sistemas dos clientes (`numero_ativo`).
* `Area`: Subdivisões de atendimento, aplicadas condicionalmente por cliente.
* `Ticket`: O chamado em si, contendo status, descrição e ID de espelhamento com o IBM Maximo (`maximo_id`).
* `TicketInteracao`: Linha do tempo de mensagens (Worklogs) e anexos trocados entre o cliente e o suporte.
* `Notificacao`: Alertas disparados para os usuários baseados em ações do sistema (ex: mudança de status).

---

## ⚙️ Integração com IBM Maximo

### 1. Criação via Email Listener
A criação inicial de tickets no Maximo é feita disparando um e-mail com um *payload* posicional estrito no corpo da mensagem. O serviço de e-mail formata os dados para que o Maximo processe as tags e gere o ticket automaticamente.

**Estrutura Obrigatória do Payload:**
<br>

Descrição do problema: {descricao_problema}<br><br> 

#MAXIMO_EMAIL_BEGIN<br>
SR#DESCRIPTION={sumario}<br>
;<br>
SR#ASSETNUM={asset_num}<br>
;<br>
SR#REPORTEDPRIORITY={prioridade}<br>
;<br>
SR#ITC_AREA={area_selecionada}<br> ;<br>
SR#LOCATION={location}<br>
;<br>
SR#AFFECTEDPERSONID={person_id}<br>
;<br>
SR#SITEID=ITCBR<br>
;<br>
LSNRACTION=CREATE<br>
;<br>
LSNRAPPLIESTO=SR<br>
;<br>
SR#CLASS=SR<br>
;<br>
SR#TICKETID=&AUTOKEY&<br>
;<br>
#MAXIMO_EMAIL_END<br><br>

### 2. Sincronização de Worklogs via API (API REST)
As interações/respostas inseridas no portal (`TicketInteracao`) são enviadas para o Maximo através de requisições REST POST (`MaximoSenderService`), sincronizando o chat do cliente diretamente com os *Worklogs* do Service Request no IBM Maximo.

---

## 💻 Instalação e Execução Local

### Pré-requisitos
* Python 3.10+
* PostgreSQL rodando localmente (ou Docker)
* Git

### Passo a Passo

1. **Clone o repositório:**
   ```bash
   git clone [https://github.com/seu-usuario/portal_suporte.git](https://github.com/seu-usuario/portal_suporte.git)
   cd portal_suporte

2. **Crie e ative o ambiente virtual:**
   ```bash
   python -m venv venv
   # Linux/MacOS:
   source venv/bin/activate
   # Windows:
   venv\Scripts\activate

3. **Instale as depêndencias:**
   ```bash
   pip install -r requirements.txt

4. **Configuração de Variáveis de Ambiente:**
* Crie um arquivo `.env` na raiz do projeto contendo as chaves necessárias (baseie-se no `settings.py`):
   ```bash
   SECRET_KEY=sua_chave_secreta_aqui
   DEBUG=True
   DB_NAME=nome_do_banco
   DB_USER=usuario_db
   DB_PASSWORD=senha_db
   DB_HOST=localhost
   DB_PORT=5432
   MAXIMO_API_KEY=sua_chave_api_maximo

5. **Execute as Migrações:**
   ```bash
   python manage.py makemigrations
   python manage.py migrate

6. **Crie o Superusuário:**
   ```bash
   python manage.py createsuperuser

7. **Inicie o Servidor Local**
   ```bash
   python manage.py runserver
   ```
   * Acesse no seu navegador: `http://localhost:8000`

---

## ☁️ Deploy (Oracle Cloud)

O sistema está configurado para operar em uma arquitetura de produção segura:

* **Gunicorn:** Atua como o servidor WSGI da aplicação Django.
* **Nginx:** Configurado como Proxy Reverso para gerenciar as requisições HTTPS e servir os arquivos estáticos (`/static/` e `/media/`).
* **Gerenciamento de Processos:** Recomenda-se o uso nativo de `systemd` no Ubuntu ou `Supervisor` para garantir a resiliência do processo do Gunicorn.

---

## 🧑‍💻 Padrões de Código e Contribuição

* **PEP 8:** Mantenha a formatação de código Python seguindo as diretrizes oficiais. Recomenda-se o uso de ferramentas como `black` e `flake8`.
* **Type Hinting:** O uso de *Type Hints* nas funções e métodos é **obrigatório** (ex: `def criar_ticket(request: HttpRequest) -> HttpResponse:`).
* **Classes Utilitárias CSS:** Utilize o padrão do projeto. Ex: `.login-container` para a tela de login, `.abrir-container` para o formulário de ticket e `.larger-card` para ajuste de responsividade.
* **Views:** Mantenha o padrão da arquitetura do projeto utilizando **Function-Based Views (FBVs)**.

---
*Documentação mantida pelo desenvolvedor da IT Consol.*
