"""Cria dados de demonstração para desenvolvimento. Idempotente.

Uso: docker compose exec web python manage.py seed_dev
"""

from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.services.models import Service
from apps.tenants.models import Tenant, TenantBusinessHours
from apps.tenants.services import create_default_business_hours

User = get_user_model()

DEMO_SERVICES = [
    ("Corte Feminino Completo", "Lavagem, corte simétrico e escova modeladora.", 60, "180.00"),
    ("Coloração Global", "Aplicação uniforme de cor, inclui hidratação básica.", 120, "350.00"),
    ("Manicure Clássica", "Cutilagem fina e esmaltação tradicional.", 45, "45.00"),
]


class Command(BaseCommand):
    help = "Cria tenant, admin e serviços de demonstração (apenas em DEBUG)."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_dev só pode rodar com DJANGO_DEBUG=True.")

        tenant, created = Tenant.objects.get_or_create(
            slug="espaco-aura",
            defaults={
                "name": "Espaço Aura",
                "whatsapp": "+5511999990000",
                "address": "Rua das Flores, 123 — São Paulo/SP",
                "description": "Estética premium com atendimento personalizado.",
            },
        )
        self.stdout.write(
            f"Tenant 'Espaço Aura': {'criado' if created else 'já existia'}"
        )
        if not TenantBusinessHours.objects.for_tenant(tenant).exists():
            create_default_business_hours(tenant)
            self.stdout.write("Horário de funcionamento padrão criado.")

        admin, created = User.objects.get_or_create(
            email="dona@espacoaura.com",
            defaults={"role": User.Role.TENANT_ADMIN, "tenant": tenant},
        )
        if created:
            admin.set_password("belezaapp123")
            admin.save(update_fields=["password"])
        self.stdout.write(
            f"Admin dona@espacoaura.com (senha: belezaapp123): "
            f"{'criado' if created else 'já existia'}"
        )

        services = {}
        for name, description, duration, price in DEMO_SERVICES:
            service, created = Service.objects.get_or_create(
                tenant=tenant,
                name=name,
                defaults={
                    "description": description,
                    "duration_minutes": duration,
                    "price": Decimal(price),
                },
            )
            services[name] = service
            self.stdout.write(
                f"Serviço '{name}': {'criado' if created else 'já existia'}"
            )

        self._seed_employees(tenant, services)
        self.stdout.write(self.style.SUCCESS("Seed concluído."))

    def _seed_employees(self, tenant, services):
        import datetime

        from apps.employees.services import (
            create_employee,
            link_service,
            set_working_hours,
        )

        demo_employees = [
            {
                "full_name": "Ana Silva",
                "email": "ana@espacoaura.com",
                "password": "belezaapp123",
                "commission": ("percentage", "40.00"),
                "services": ["Corte Feminino Completo", "Coloração Global"],
                "weekdays": [0, 1, 2, 3, 4],
            },
            {
                "full_name": "Júlia Mendes",
                "email": "julia@espacoaura.com",
                "password": "belezaapp123",
                "commission": ("fixed", "20.00"),
                "services": ["Manicure Clássica"],
                "weekdays": [2, 3, 4, 5],
            },
        ]
        for spec in demo_employees:
            if User.objects.filter(email=spec["email"]).exists():
                self.stdout.write(f"Funcionário '{spec['full_name']}': já existia")
                continue
            employee = create_employee(
                tenant=tenant,
                full_name=spec["full_name"],
                email=spec["email"],
                password=spec["password"],
                default_commission_type=spec["commission"][0],
                default_commission_value=Decimal(spec["commission"][1]),
            )
            set_working_hours(
                employee,
                [
                    {
                        "weekday": weekday,
                        "start_time": datetime.time(9, 0),
                        "end_time": datetime.time(18, 0),
                    }
                    for weekday in spec["weekdays"]
                ],
            )
            for service_name in spec["services"]:
                link_service(employee, services[service_name])
            self.stdout.write(
                f"Funcionário '{spec['full_name']}' (senha: {spec['password']}): criado"
            )
