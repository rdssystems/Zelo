"""Wrapper fino sobre a API REST do Asaas (billing da plataforma).

Nenhuma regra de negócio aqui — só request/response. Quem decide o que fazer
com o resultado é `apps/billing/services.py`. Chave/token sempre vêm do
`.env` (`ASAAS_API_KEY`, `ASAAS_WEBHOOK_TOKEN`, `ASAAS_ENV`) — nunca hardcoded.

Enquanto `ASAAS_API_KEY` estiver vazio no `.env` (ambiente sem credencial
ainda), toda função aqui levanta `AsaasNotConfigured` — o resto do sistema já
está pronto, só falta a chave pra funcionar de verdade.
"""

import requests
from django.conf import settings

ASAAS_TIMEOUT = 15


class AsaasError(Exception):
    """Erro de comunicação com o Asaas (rede, resposta de erro da API)."""


class AsaasNotConfigured(AsaasError):
    """`ASAAS_API_KEY` não preenchida no `.env` — integração pronta, só falta a chave."""


def _base_url():
    if settings.ASAAS_ENV == "production":
        return "https://api.asaas.com/v3"
    return "https://api-sandbox.asaas.com/v3"


def _headers():
    if not settings.ASAAS_API_KEY:
        raise AsaasNotConfigured(
            "ASAAS_API_KEY não configurada no .env — preencha com a chave de "
            "sandbox ou produção do Asaas pra ativar cobrança de verdade."
        )
    return {
        "access_token": settings.ASAAS_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": "Zellup",
    }


def _request(method, path, **kwargs):
    url = f"{_base_url()}{path}"
    try:
        response = requests.request(
            method, url, headers=_headers(), timeout=ASAAS_TIMEOUT, **kwargs
        )
    except requests.RequestException as exc:
        raise AsaasError(f"Falha de rede ao chamar o Asaas ({path}): {exc}") from exc

    if response.status_code >= 400:
        detail = response.text
        try:
            body = response.json()
            errors = body.get("errors")
            if errors:
                detail = "; ".join(e.get("description", "") for e in errors)
        except ValueError:
            pass
        raise AsaasError(f"Asaas retornou {response.status_code} em {path}: {detail}")

    return response.json() if response.content else {}


def create_customer(*, name, cpf_cnpj, email="", external_reference=""):
    """Cria (ou, se já existir, ainda cria outro — dedupe fica por conta de
    quem chama, guardando o `id` retornado em `Subscription.asaas_customer_id`
    depois da 1ª chamada) o cliente no Asaas."""
    payload = {
        "name": name,
        "cpfCnpj": "".join(ch for ch in cpf_cnpj if ch.isdigit()),
        "externalReference": external_reference,
    }
    if email:
        payload["email"] = email
    return _request("POST", "/customers", json=payload)


def create_subscription(*, customer_id, value, next_due_date, description="", external_reference=""):
    """`billingType=UNDEFINED` deixa o cliente escolher PIX/boleto/cartão na
    própria página da fatura (`invoiceUrl`) — é o que permite o pagamento
    ficar "dentro" do fluxo do Zellup (1 link só, sem o admin do salão decidir
    o meio de pagamento por nós)."""
    payload = {
        "customer": customer_id,
        "billingType": "UNDEFINED",
        "value": str(value),
        "nextDueDate": next_due_date.isoformat(),
        "cycle": "MONTHLY",
        "description": description,
        "externalReference": external_reference,
    }
    return _request("POST", "/subscriptions", json=payload)


def get_subscription_payments(asaas_subscription_id):
    """Lista as cobranças (`Payment`) já geradas pra essa assinatura — usado
    logo após criar a assinatura pra pegar a `invoiceUrl` da 1ª fatura."""
    data = _request("GET", f"/subscriptions/{asaas_subscription_id}/payments")
    return data.get("data", [])


def get_first_invoice_url(asaas_subscription_id):
    payments = get_subscription_payments(asaas_subscription_id)
    if not payments:
        return None
    return payments[0].get("invoiceUrl")
