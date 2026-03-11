# 🛠️ Portal de Suporte - IT Consol

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![Django](https://img.shields.io/badge/Django-5.x-green.svg)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Ready-blue.svg)
![Bootstrap](https://img.shields.io/badge/Bootstrap-5.3-purple.svg)

Um portal web robusto desenvolvido para clientes realizarem abertura, acompanhamento e interação em chamados (tickets) de suporte. O sistema atua como um frontend amigável que possui integração direta e automatizada com o **IBM Maximo** através de um Email Listener customizado e rotinas de sincronização.

---

## 🚀 Tecnologias e Stack

* **Backend:** Python 3.x, Django 5.x
* **Banco de Dados:** PostgreSQL
* **Frontend:** HTML5, CSS3 Customizado, Bootstrap 5.3
* **Tipografia Visual:** Fonte IBM Plex Mono (Padrão visual corporativo)
* **Infraestrutura:** Oracle Cloud Infrastructure (OCI) - Ubuntu VM (1 vCPU, 6 GB RAM)
* **Servidor Web:** Gunicorn + Nginx

---

## ✨ Principais Funcionalidades e Segurança

* **Autenticação Customizada & Segurança:** Login via E-mail (*Case Insensitive*) utilizando um `ModelBackend` próprio. Inclui mitigação ativa contra *Timing Attacks* na enumeração de usuários.
* **Gestão de Anexos Segura:** Sistema robusto de validação de arquivos (`_validar_anexo_comum`), limitando uploads a 150MB e restrição estrita de *MIME types* e extensões.
* **Lógica de Negócio Dinâmica:** O campo "Área" (`Area`) é exibido condicionalmente para clientes específicos (ex: empresas PAMPA ou ABL) baseado no domínio/vínculo do e-mail.
* **Comunicação Bidirecional:** Chat interno no ticket (`TicketInteracao`) com upload de evidências e histórico imutável para auditoria.
* **Notificações In-App:** Alertas em tempo real de mudanças de status do ticket (`monitorar_mudancas_ticket` via Signals) e novas mensagens.

---

## 📂 Arquitetura e Modelos Principais

O sistema está centrado no app `tickets`. Os principais modelos de dados (`models.py`) são:

* `Cliente`: Estende `AbstractUser`. Autentica via e-mail e possui campos atrelados ao Maximo (`location`, `affected_person_id`).
* `Ambiente`: Representa os ativos/sistemas dos clientes.
* `Area`: Subdivisões de atendimento, aplicadas condicionalmente por cliente.
* `Ticket`: O chamado em si, contendo status, SLA, prioridade e ID de espelhamento com o IBM Maximo.
* `TicketInteracao`: Linha do tempo de mensagens (Worklogs) e anexos trocados entre o cliente e o suporte.
* `Notificacao`: Alertas disparados para os usuários baseados em ações do sistema.

---

## ⚙️ Integração com IBM Maximo

### 1. Criação via Email Listener
A criação de tickets no Maximo é feita disparando um e-mail com um *payload* posicional estrito.

```html
Descrição do problema: {descricao_problema}<br><br> 

#MAXIMO_EMAIL_BEGIN<br>
SR#DESCRIPTION={sumario}<br>;<br>
SR#ASSETNUM={asset_num}<br>;<br>
SR#REPORTEDPRIORITY={prioridade}<br>;<br>
SR#ITC_AREA={area_selecionada}<br>;<br> SR#LOCATION={location}<br>;<br>
SR#AFFECTEDPERSONID={person_id}<br>;<br> SR#SITEID=ITCBR<br>;<br>
LSNRACTION=CREATE<br>;<br>
LSNRAPPLIESTO=SR<br>;<br>
SR#CLASS=SR<br>;<br>
SR#TICKETID=&AUTOKEY&<br>;<br>
#MAXIMO_EMAIL_END<br><br>
