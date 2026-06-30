# SDD Progress — tratamento de erros na criação de ticket

Plan: docs/superpowers/plans/2026-06-30-erros-criacao-ticket.md
BASE commit: da4dcc6
No-commit mode: implementers NAO commitam (so o Gustavo commita); review via diff do working tree.

Task 1: complete (view retry_com_erro + cópia reforçada; template remove override messages + banner reanexo + alerta lista todos non_field_errors; ErrosCriacaoTicketTests 3/3 pass, suite 94 OK; review Spec 6/6 + Quality Approved).
  ⚠️ resolvido: rollback depende de transaction.atomic() em views.py:299 (pre-existente, confirmado).
  Minor p/ review final: test_form_invalido nao assere texto do alerta (so id+banner); sem acao.

TASK COMPLETA.
