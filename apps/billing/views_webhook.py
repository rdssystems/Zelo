"""Webhook do Asaas — POST /webhooks/asaas/ (fora de /painel/ e /plataforma/
de propósito: chamado pelo servidor do Asaas, não por um usuário logado).

Validação de segurança: o Asaas manda o `authToken` configurado no cadastro
do webhook (painel do Asaas) no header `asaas-access-token` — comparamos com
`ASAAS_WEBHOOK_TOKEN` do `.env`. Sem isso, qualquer um poderia forjar um
POST "PAYMENT_CONFIRMED" e ativar assinatura de graça.
"""

import json
import logging

from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from django.conf import settings

from . import services as billing_ops

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def asaas_webhook(request):
    token = request.headers.get("asaas-access-token", "")
    if not settings.ASAAS_WEBHOOK_TOKEN or token != settings.ASAAS_WEBHOOK_TOKEN:
        logger.warning("Webhook Asaas rejeitado: token ausente ou inválido.")
        return HttpResponseForbidden("Token inválido.")

    try:
        body = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=400)

    event_type = body.get("event", "")
    payment_data = body.get("payment") or {}
    if event_type and payment_data:
        billing_ops.handle_asaas_webhook(event_type=event_type, payment_data=payment_data)

    # Sempre 200 pra evento reconhecido mas não tratado (ex: evento de outro
    # tipo, cobrança avulsa sem subscription) — só rejeitamos por token
    # inválido. Responder erro aqui faria o Asaas ficar reenviando à toa.
    return HttpResponse(status=200)
