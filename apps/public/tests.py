import datetime
from decimal import Decimal

from django.test import TestCase, override_settings

from apps.clients.models import Client
from apps.employees.services import create_employee, link_service, set_working_hours
from apps.scheduling.models import Appointment, AppointmentStatus
from apps.services.services import create_service, set_service_active
from apps.tenants.models import Tenant

# django-ratelimit usa o cache padrão (Redis em produção/dev) para contar
# tentativas — isolar em memória evita que reruns da suíte no mesmo host
# acumulem contagem entre execuções e quebrem testes não relacionados.
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def make_tenant(slug):
    return Tenant.objects.create(name=f"Salão {slug}", slug=slug)


def make_bookable_setup(tenant, email="ana@tenant.com"):
    """Funcionário ativo, vinculado a um serviço, com jornada todo santo dia
    (facilita os testes: qualquer data futura tem horário livre)."""
    employee = create_employee(
        tenant=tenant,
        full_name="Ana Silva",
        email=email,
        password="Senha@123",
        default_commission_type="percentage",
        default_commission_value=Decimal("40.00"),
    )
    service = create_service(
        tenant=tenant, name="Corte", duration_minutes=60, price=Decimal("100.00")
    )
    link_service(employee, service)
    set_working_hours(
        employee,
        [
            {"weekday": wd, "start_time": datetime.time(9, 0), "end_time": datetime.time(18, 0)}
            for wd in range(7)
        ],
    )
    return employee, service


@override_settings(CACHES=LOCMEM_CACHE)
class BookingFlowEndToEndTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = make_tenant("salao-a")
        cls.employee, cls.service = make_bookable_setup(cls.tenant, "ana@salao-a.com")
        cls.tomorrow = datetime.date.today() + datetime.timedelta(days=1)

    def _identify_url(self, date):
        return (
            f"/{self.tenant.slug}/agendar/identificar/?service={self.service.pk}"
            f"&employee={self.employee.pk}&date={date.isoformat()}&time=09:00"
        )

    def test_home_page_shows_tenant_identity(self):
        response = self.client.get(f"/{self.tenant.slug}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.tenant.name)

    def test_whatsapp_button_prefixes_ddi_55(self):
        """Regressão: o botão "Fale conosco" só tirava pontuação do número
        digitado (`|cut:...`), sem garantir o DDI 55 — um número como
        "+34 997 64 88 92" (DDD 34 de Uberlândia/MG, sem DDI) virava
        "34997648892" no link `wa.me`, que o WhatsApp interpretava como
        código do país da Espanha (+34) em vez do DDD brasileiro, e dava
        "número não está no WhatsApp". `whatsapp_wa_me_number` já resolve
        isso (só dígitos + garante o 55 na frente) — o bug era o template
        não usar essa property."""
        self.tenant.whatsapp = "+34 997 64 88 92"
        self.tenant.save(update_fields=["whatsapp"])
        response = self.client.get(f"/{self.tenant.slug}/")
        self.assertContains(response, "https://wa.me/5534997648892")
        self.assertNotContains(response, "wa.me/34997648892")

    def test_whatsapp_button_hidden_without_tenant_whatsapp(self):
        self.tenant.whatsapp = ""
        self.tenant.save(update_fields=["whatsapp"])
        response = self.client.get(f"/{self.tenant.slug}/")
        self.assertNotContains(response, "wa.me/")

    def test_service_step_lists_bookable_service(self):
        response = self.client.get(f"/{self.tenant.slug}/agendar/")
        self.assertContains(response, "Corte")

    def test_employee_step_lists_linked_employee(self):
        response = self.client.get(
            f"/{self.tenant.slug}/agendar/profissional/?service={self.service.pk}"
        )
        self.assertContains(response, "Ana Silva")

    def test_schedule_step_shows_available_time(self):
        response = self.client.get(
            f"/{self.tenant.slug}/agendar/horario/?service={self.service.pk}&employee={self.employee.pk}"
        )
        self.assertContains(response, "09:00")

    def test_new_phone_requires_name_before_creating_client(self):
        response = self.client.post(
            self._identify_url(self.tomorrow), {"phone": "11987654321"}
        )
        self.assertContains(response, "Seu nome")
        self.assertFalse(Client.objects.filter(phone="11987654321").exists())

    def test_new_client_can_optionally_set_birthday(self):
        response = self.client.post(
            self._identify_url(self.tomorrow),
            {"phone": "11987654321", "name": "Maria Cliente", "birth_day": "15", "birth_month": "6"},
        )
        self.assertEqual(response.status_code, 302)
        client_ = Client.objects.get(tenant=self.tenant, phone="11987654321")
        self.assertEqual(client_.birth_day, 15)
        self.assertEqual(client_.birth_month, 6)

    def test_new_client_without_birthday_is_fine(self):
        response = self.client.post(
            self._identify_url(self.tomorrow),
            {"phone": "11987654321", "name": "Maria Cliente"},
        )
        self.assertEqual(response.status_code, 302)
        client_ = Client.objects.get(tenant=self.tenant, phone="11987654321")
        self.assertIsNone(client_.birth_day)
        self.assertIsNone(client_.birth_month)

    def test_new_client_with_only_birth_day_shows_error_and_keeps_name(self):
        response = self.client.post(
            self._identify_url(self.tomorrow),
            {"phone": "11987654321", "name": "Maria Cliente", "birth_day": "15"},
        )
        self.assertContains(response, "Informe dia e mês")
        # não perde o nome já digitado ao reexibir o formulário
        self.assertContains(response, 'value="Maria Cliente"')
        self.assertFalse(Client.objects.filter(phone="11987654321").exists())

    def test_full_flow_creates_pending_appointment(self):
        identify_url = self._identify_url(self.tomorrow)

        response = self.client.post(
            identify_url, {"phone": "11987654321", "name": "Maria Cliente"}
        )
        self.assertEqual(response.status_code, 302)
        client_ = Client.objects.get(tenant=self.tenant, phone="11987654321")
        self.assertEqual(client_.name, "Maria Cliente")

        confirm_url = response.url
        response = self.client.post(confirm_url, {"phone": "11987654321"})
        self.assertEqual(response.status_code, 302)

        appointment = Appointment.objects.get(client=client_)
        self.assertEqual(appointment.status, AppointmentStatus.PENDING)
        self.assertEqual(appointment.employee, self.employee)
        self.assertEqual(appointment.service, self.service)
        self.assertEqual(appointment.date, self.tomorrow)
        self.assertEqual(appointment.start_time, datetime.time(9, 0))
        self.assertEqual(appointment.price_at_booking, self.service.price)

        success_response = self.client.get(response.url)
        self.assertContains(success_response, "Agendamento Enviado")
        self.assertContains(success_response, "Corte")

    def test_full_flow_auto_confirms_when_tenant_enables_it(self):
        self.tenant.auto_confirm_appointments = True
        self.tenant.save(update_fields=["auto_confirm_appointments"])
        identify_url = self._identify_url(self.tomorrow)

        response = self.client.post(
            identify_url, {"phone": "11987654322", "name": "Joana Cliente"}
        )
        confirm_url = response.url
        response = self.client.post(confirm_url, {"phone": "11987654322"})

        appointment = Appointment.objects.get(client__phone="11987654322")
        self.assertEqual(appointment.status, AppointmentStatus.CONFIRMED)

        success_response = self.client.get(response.url)
        self.assertContains(success_response, "Agendamento Confirmado")

    def test_repeated_phone_recovers_client_instead_of_duplicating(self):
        existing = Client.objects.create(
            tenant=self.tenant, phone="11999998888", name="Cliente Existente"
        )
        response = self.client.post(
            self._identify_url(self.tomorrow), {"phone": "11999998888"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"client={existing.pk}", response.url)
        self.assertEqual(Client.objects.filter(phone="11999998888").count(), 1)

    def test_slot_taken_between_listing_and_identifying(self):
        other_client = Client.objects.create(
            tenant=self.tenant, phone="11911112222", name="Outro"
        )
        Appointment.objects.create(
            tenant=self.tenant,
            client=other_client,
            employee=self.employee,
            service=self.service,
            date=self.tomorrow,
            start_time=datetime.time(9, 0),
            end_time=datetime.time(10, 0),
            status=AppointmentStatus.CONFIRMED,
            price_at_booking=self.service.price,
        )
        response = self.client.get(self._identify_url(self.tomorrow))
        self.assertContains(response, "acabou de ser reservado")


class BookingValidationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = make_tenant("salao-b")
        cls.employee, cls.service = make_bookable_setup(cls.tenant, "ana@salao-b.com")
        cls.inactive_service = create_service(
            tenant=cls.tenant, name="Inativo", duration_minutes=30, price=Decimal("50")
        )
        set_service_active(cls.inactive_service, False)

    def test_non_bookable_service_404s(self):
        response = self.client.get(
            f"/{self.tenant.slug}/agendar/profissional/?service={self.inactive_service.pk}"
        )
        self.assertEqual(response.status_code, 404)

    def test_employee_not_linked_to_service_404s(self):
        other_employee = create_employee(
            tenant=self.tenant,
            full_name="Bia",
            email="bia@salao-b.com",
            password="Senha@123",
            default_commission_type="percentage",
            default_commission_value=Decimal("40"),
        )
        response = self.client.get(
            f"/{self.tenant.slug}/agendar/horario/?service={self.service.pk}&employee={other_employee.pk}"
        )
        self.assertEqual(response.status_code, 404)

    def test_cross_tenant_service_not_bookable(self):
        other_tenant = make_tenant("salao-c")
        response = self.client.get(
            f"/{other_tenant.slug}/agendar/profissional/?service={self.service.pk}"
        )
        self.assertEqual(response.status_code, 404)


class MyAppointmentsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = make_tenant("salao-d")
        cls.employee, cls.service = make_bookable_setup(cls.tenant, "ana@salao-d.com")
        cls.client_ = Client.objects.create(
            tenant=cls.tenant, phone="11955556666", name="Fernanda"
        )
        cls.tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        cls.appointment = Appointment.objects.create(
            tenant=cls.tenant,
            client=cls.client_,
            employee=cls.employee,
            service=cls.service,
            date=cls.tomorrow,
            start_time=datetime.time(9, 0),
            end_time=datetime.time(10, 0),
            status=AppointmentStatus.PENDING,
            price_at_booking=cls.service.price,
        )

    def test_lookup_by_phone_shows_appointment(self):
        response = self.client.post(
            f"/{self.tenant.slug}/meus-agendamentos/", {"phone": "11955556666"}
        )
        self.assertContains(response, "Fernanda")
        self.assertContains(response, "Corte")

    def test_lookup_unknown_phone_shows_error(self):
        response = self.client.post(
            f"/{self.tenant.slug}/meus-agendamentos/", {"phone": "11900000000"}
        )
        self.assertContains(response, "Não encontramos")

    def test_cancel_requires_verified_session(self):
        # sem antes ter buscado pelo telefone — não pode cancelar direto pela URL
        response = self.client.post(
            f"/{self.tenant.slug}/meus-agendamentos/{self.appointment.pk}/cancelar/"
        )
        self.assertEqual(response.status_code, 404)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, AppointmentStatus.PENDING)

    def test_cancel_after_verification_works(self):
        self.client.post(
            f"/{self.tenant.slug}/meus-agendamentos/", {"phone": "11955556666"}
        )
        response = self.client.post(
            f"/{self.tenant.slug}/meus-agendamentos/{self.appointment.pk}/cancelar/"
        )
        self.assertEqual(response.status_code, 200)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, AppointmentStatus.CANCELED)

    def test_cannot_cancel_another_clients_appointment(self):
        other_client = Client.objects.create(
            tenant=self.tenant, phone="11933334444", name="Outra"
        )
        other_appt = Appointment.objects.create(
            tenant=self.tenant,
            client=other_client,
            employee=self.employee,
            service=self.service,
            date=self.tomorrow,
            start_time=datetime.time(11, 0),
            end_time=datetime.time(12, 0),
            status=AppointmentStatus.PENDING,
            price_at_booking=self.service.price,
        )
        self.client.post(
            f"/{self.tenant.slug}/meus-agendamentos/", {"phone": "11955556666"}
        )
        response = self.client.post(
            f"/{self.tenant.slug}/meus-agendamentos/{other_appt.pk}/cancelar/"
        )
        self.assertEqual(response.status_code, 404)
        other_appt.refresh_from_db()
        self.assertEqual(other_appt.status, AppointmentStatus.PENDING)

    def test_cancel_marks_canceled_by_client_and_notifies_tenant(self):
        from apps.notifications.models import TenantNotification

        self.client.post(f"/{self.tenant.slug}/meus-agendamentos/", {"phone": "11955556666"})
        self.client.post(f"/{self.tenant.slug}/meus-agendamentos/{self.appointment.pk}/cancelar/")
        self.appointment.refresh_from_db()
        self.assertTrue(self.appointment.canceled_by_client)
        self.assertTrue(TenantNotification.objects.filter(tenant=self.tenant).exists())

    def test_cancel_confirm_shows_whatsapp_redirect_when_enabled_and_configured(self):
        self.tenant.whatsapp = "11988887777"
        self.tenant.save(update_fields=["whatsapp"])
        self.client.post(f"/{self.tenant.slug}/meus-agendamentos/", {"phone": "11955556666"})
        response = self.client.get(
            f"/{self.tenant.slug}/meus-agendamentos/{self.appointment.pk}/cancelar/confirmar/"
        )
        self.assertContains(response, "wa.me/5511988887777")
        self.assertContains(response, "window.open(")

    def test_cancel_confirm_no_redirect_without_tenant_whatsapp(self):
        # cls.tenant nasce sem whatsapp cadastrado (make_tenant não seta)
        self.client.post(f"/{self.tenant.slug}/meus-agendamentos/", {"phone": "11955556666"})
        response = self.client.get(
            f"/{self.tenant.slug}/meus-agendamentos/{self.appointment.pk}/cancelar/confirmar/"
        )
        self.assertNotContains(response, "wa.me/")

    def test_cancel_confirm_no_redirect_when_toggle_disabled(self):
        self.tenant.whatsapp = "11988887777"
        self.tenant.whatsapp_cancel_redirect_enabled = False
        self.tenant.save(update_fields=["whatsapp", "whatsapp_cancel_redirect_enabled"])
        self.client.post(f"/{self.tenant.slug}/meus-agendamentos/", {"phone": "11955556666"})
        response = self.client.get(
            f"/{self.tenant.slug}/meus-agendamentos/{self.appointment.pk}/cancelar/confirmar/"
        )
        self.assertNotContains(response, "wa.me/")

    def test_pending_appointment_shows_awaiting_confirmation_badge(self):
        response = self.client.post(
            f"/{self.tenant.slug}/meus-agendamentos/", {"phone": "11955556666"}
        )
        self.assertContains(response, "Aguardando confirmação")

    def test_confirmed_appointment_shows_confirmed_badge(self):
        self.appointment.status = AppointmentStatus.CONFIRMED
        self.appointment.save(update_fields=["status"])
        response = self.client.post(
            f"/{self.tenant.slug}/meus-agendamentos/", {"phone": "11955556666"}
        )
        self.assertContains(response, "Confirmado")
        self.assertNotContains(response, "Aguardando confirmação")


@override_settings(CACHES=LOCMEM_CACHE)
class RateLimitTest(TestCase):
    """02-ARQUITETURA.md §8: rate limit por IP + telefone no endpoint de
    criação de agendamento."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant = make_tenant("salao-e")
        cls.employee, cls.service = make_bookable_setup(cls.tenant, "ana@salao-e.com")
        cls.client_ = Client.objects.create(
            tenant=cls.tenant, phone="11922223333", name="Repetida"
        )

    def _confirm_url(self, date):
        return (
            f"/{self.tenant.slug}/agendar/confirmar/?service={self.service.pk}"
            f"&employee={self.employee.pk}&date={date.isoformat()}"
            f"&time=09:00:00&client={self.client_.pk}"
        )

    def test_same_phone_blocked_after_five_confirmations_per_hour(self):
        base_date = datetime.date.today() + datetime.timedelta(days=1)
        for i in range(5):
            date = base_date + datetime.timedelta(days=i)
            response = self.client.post(self._confirm_url(date), {"phone": "11922223333"})
            self.assertEqual(
                response.status_code, 302, f"tentativa {i + 1} deveria ser aceita"
            )

        sixth_date = base_date + datetime.timedelta(days=5)
        response = self.client.post(self._confirm_url(sixth_date), {"phone": "11922223333"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Muitas tentativas")
        self.assertEqual(Appointment.objects.filter(client=self.client_).count(), 5)


class ReservedSlugTest(TestCase):
    def test_reserved_slug_rejected_on_full_clean(self):
        tenant = Tenant(name="Painel Ltda", slug="painel")
        with self.assertRaises(Exception):
            tenant.full_clean()
