import os
import io
import django
from PIL import Image, ImageDraw
from django.core.files.base import ContentFile

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.accounts.models import User
from apps.tenants.models import Tenant, TenantBusinessHours
from apps.employees.models import Employee, WorkingHours, EmployeeService
from apps.services.models import Service
from apps.clients.models import Client

email = "klismanrds90@gmail.com"
user = User.objects.filter(email=email).first()
if not user:
    user = User.objects.create_user(
        email=email,
        first_name="Klisman",
        last_name="Ramos",
        role="tenant_admin"
    )

tenant = user.tenant
if not tenant:
    tenant = Tenant.objects.create(
        name="Barbearia Ramos",
        slug="barbearia-ramos",
        whatsapp="11987654321",
        address="Rua dos Barbeiros, 150 - Centro",
        description="Barbearia clássica e moderna. Atendimento exclusivo, cerveja gelada, sinuca e os melhores profissionais da região. O cuidado que seu estilo merece.",
        theme="barbearia",
        auto_confirm_appointments=True
    )
    user.tenant = tenant
    user.save()
else:
    tenant.name = "Barbearia Ramos"
    tenant.slug = "barbearia-ramos"
    tenant.whatsapp = "11987654321"
    tenant.address = "Rua dos Barbeiros, 150 - Centro"
    tenant.description = "Barbearia clássica e moderna. Atendimento exclusivo, cerveja gelada, sinuca e os melhores profissionais da região. O cuidado que seu estilo merece."
    tenant.theme = "barbearia"
    tenant.auto_confirm_appointments = True
    tenant.save()

for day in range(7):
    TenantBusinessHours.objects.get_or_create(
        tenant=tenant,
        weekday=day,
        defaults={
            'start_time': '09:00:00',
            'end_time': '20:00:00',
            'is_closed': (day == 6)
        }
    )

def create_logo_image():
    img = Image.new('RGB', (400, 400), color='#17130F')
    draw = ImageDraw.Draw(img)
    draw.ellipse([20, 20, 380, 380], outline='#FBBA64', width=6)
    draw.ellipse([30, 30, 370, 370], outline='#2A231C', width=3)
    draw.rectangle([180, 70, 220, 230], fill='#A33A3A', outline='#FBBA64', width=2)
    draw.line([180, 90, 220, 110], fill='#FFFFFF', width=5)
    draw.line([180, 130, 220, 150], fill='#3A5AA3', width=5)
    draw.line([180, 170, 220, 190], fill='#FFFFFF', width=5)
    draw.rectangle([60, 260, 340, 320], fill='#FBBA64')
    draw.rectangle([65, 265, 335, 315], fill='#17130F')
    draw.text((120, 280), "BARBEARIA RAMOS", fill='#FBBA64')
    draw.text((150, 335), "EST. 2026", fill='#8C8075')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return ContentFile(buf.getvalue(), name='barbearia_logo.png')

def create_cover_image():
    img = Image.new('RGB', (1200, 400), color='#1F1914')
    draw = ImageDraw.Draw(img)
    for i in range(0, 1200, 40):
        draw.line([i, 0, i+200, 400], fill='#28201A', width=2)
    draw.rectangle([40, 40, 1160, 360], outline='#FBBA64', width=3)
    draw.rectangle([50, 50, 1150, 350], outline='#3A2E25', width=1)
    draw.text((380, 160), "BARBEARIA RAMOS", fill='#FBBA64')
    draw.text((410, 210), "CORTE * BARBA * TRADICAO & ESTILO", fill='#E6DCD3')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return ContentFile(buf.getvalue(), name='barbearia_cover.png')

def create_background_image():
    img = Image.new('RGB', (1920, 1080), color='#120E0B')
    draw = ImageDraw.Draw(img)
    for y in range(0, 1080, 20):
        c = int(18 - (y / 1080) * 8)
        hex_c = f"#{c:02x}{c-3:02x}{c-5:02x}"
        draw.line([0, y, 1920, y], fill=hex_c, width=20)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return ContentFile(buf.getvalue(), name='barbearia_background.png')

def create_employee_photo(name, initials):
    img = Image.new('RGB', (300, 300), color='#251E18')
    draw = ImageDraw.Draw(img)
    draw.ellipse([10, 10, 290, 290], outline='#FBBA64', width=4)
    draw.text((120, 130), initials, fill='#FBBA64')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return ContentFile(buf.getvalue(), name=f'emp_{initials}.png')

tenant.logo.save('barbearia_logo.png', create_logo_image(), save=False)
tenant.cover_image.save('barbearia_cover.png', create_cover_image(), save=False)
tenant.background_image.save('barbearia_background.png', create_background_image(), save=False)
tenant.save()

emp1_user, _ = User.objects.get_or_create(
    email="lucas.barber@barbeariaramos.com",
    defaults={'first_name': 'Lucas', 'last_name': 'Santos', 'role': 'employee', 'tenant': tenant}
)
if emp1_user.tenant != tenant:
    emp1_user.tenant = tenant
    emp1_user.save()

emp1, _ = Employee.objects.get_or_create(
    tenant=tenant,
    user=emp1_user,
    defaults={
        'full_name': 'Lucas "Navalha" Santos',
        'bio': 'Especialista em degradê navalhado, freestyle e alinhamento de barba na toalha quente.',
        'phone': '11977771111',
        'default_commission_type': 'percentage',
        'default_commission_value': 50.00,
        'is_active': True
    }
)
emp1.photo.save('lucas_navalha.png', create_employee_photo('Lucas', 'LN'), save=True)

emp2_user, _ = User.objects.get_or_create(
    email="gabriel.barber@barbeariaramos.com",
    defaults={'first_name': 'Gabriel', 'last_name': 'Oliveira', 'role': 'employee', 'tenant': tenant}
)
if emp2_user.tenant != tenant:
    emp2_user.tenant = tenant
    emp2_user.save()

emp2, _ = Employee.objects.get_or_create(
    tenant=tenant,
    user=emp2_user,
    defaults={
        'full_name': 'Gabriel "Barber" Oliveira',
        'bio': 'Mestre em cortes clássicos na tesoura, pigmentação e tratamentos capilares masculinos.',
        'phone': '11977772222',
        'default_commission_type': 'percentage',
        'default_commission_value': 45.00,
        'is_active': True
    }
)
emp2.photo.save('gabriel_barber.png', create_employee_photo('Gabriel', 'GB'), save=True)

for emp in [emp1, emp2]:
    for day in range(0, 6):
        WorkingHours.objects.get_or_create(
            tenant=tenant,
            employee=emp,
            weekday=day,
            defaults={
                'start_time': '09:00:00',
                'end_time': '19:00:00',
                'is_active': True
            }
        )

services_data = [
    {
        'name': 'Corte Masculino (Degradê / Tesoura)',
        'description': 'Corte moderno ou clássico com lavagem inclusa e finalização com pomada premium.',
        'duration_minutes': 30,
        'price': 45.00
    },
    {
        'name': 'Barba Tradicional (Toalha Quente)',
        'description': 'Barba desenhada com navalha, vaporizador/toalha quente e pós-barba hidratante.',
        'duration_minutes': 30,
        'price': 35.00
    },
    {
        'name': 'Combo VIP (Cabelo + Barba)',
        'description': 'O pacote completo de estilo: corte caprichado + barba na toalha quente + cerveja trincando.',
        'duration_minutes': 50,
        'price': 70.00
    },
    {
        'name': 'Pezinho & Sobrancelha',
        'description': 'Acabamento do pezinho com navalha e alinhamento de sobrancelha.',
        'duration_minutes': 15,
        'price': 20.00
    },
    {
        'name': 'Pigmentação de Barba ou Cabelo',
        'description': 'Disfarce de falhas e grisalhos para um visual mais marcante e alinhado.',
        'duration_minutes': 25,
        'price': 35.00
    },
    {
        'name': 'Hidratação Capilar & Lavagem Especial',
        'description': 'Tratamento profundo para fios ressecados e couro cabeludo.',
        'duration_minutes': 25,
        'price': 40.00
    }
]

for s_data in services_data:
    srv, _ = Service.objects.get_or_create(
        tenant=tenant,
        name=s_data['name'],
        defaults=s_data
    )
    for emp in [emp1, emp2]:
        EmployeeService.objects.get_or_create(
            tenant=tenant,
            employee=emp,
            service=srv,
            defaults={
                'commission_type': 'percentage',
                'commission_value': emp.default_commission_value
            }
        )

clients_data = [
    ('Carlos Eduardo Silva', '11991234567', 'Gosta de degradê navalhado baixo, toma cerveja IPA'),
    ('Matheus Henrique Rocha', '11998765432', 'Barba alinhada na navalha, corte só na tesoura'),
    ('Felipe Augusto Lima', '11981112233', 'Cliente mensalista, vem todas as sextas-feiras'),
    ('Rodrigo Alves Costa', '11972223344', 'Pigmentação na barba, gosta de atendimento rápido'),
    ('Thiago Martins Ferreira', '11963334455', 'Combo Cabelo + Barba, prefere o barbeiro Lucas'),
    ('Bruno Souza Ribeiro', '11954445566', 'Vem quinzenalmente para pezinho e sobrancelha'),
    ('Guilherme Prado Santos', '11945556677', 'Hidratação capilar e lavagem especial'),
    ('Alexandre Nogueira', '11936667788', 'Corte clássico social para trabalho'),
    ('Lucas Gabriel Barbosa', '11927778899', 'Cabelo freestyle e barba desenhada'),
    ('Daniel Carvalho Mendes', '11918889900', 'Prefere atendimento com o barbeiro Gabriel'),
    ('Rafael Viana Oliveira', '11909990011', 'Traz o filho para cortar junto'),
    ('Vinícius Teixeira', '11998881122', 'Barba completa com ritual de toalha quente'),
    ('Marcelo Augusto Ramos', '11987772233', 'Cliente antigo, gosta de bater papo'),
    ('Diego Henrique Cardoso', '11976663344', 'Vem sempre aos sábados de manhã cedo'),
    ('Leonardo Gomes Faria', '11965554455', 'Combo completo: cabelo, barba e sobrancelha')
]

for name, phone, pref in clients_data:
    Client.objects.get_or_create(
        tenant=tenant,
        phone=phone,
        defaults={
            'name': name,
            'preferences': pref
        }
    )

print("SUCCESS: Barbearia tenant, images, employees, services and clients populated successfully!")
