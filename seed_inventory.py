import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.accounts.models import User
from apps.tenants.models import Tenant
from apps.inventory.models import Supplier, Category, Product, StockMovement

tenant = Tenant.objects.get(slug='barbearia-ramos')
user = User.objects.get(email='klismanrds90@gmail.com')

# Create Suppliers
fornecedores_data = [
    {'name': 'Beleza Profissional Distribuidora', 'contact_name': 'Carlos', 'phone': '11999998888', 'email': 'vendas@belezaprof.com'},
    {'name': 'Barber Shop Suprimentos', 'contact_name': 'Roberto', 'phone': '11988887777', 'email': 'roberto@barbershop.com'}
]

fornecedores = {}
for data in fornecedores_data:
    forn, _ = Supplier.objects.get_or_create(tenant=tenant, name=data['name'], defaults=data)
    fornecedores[data['name']] = forn

# Create Categories
categorias_data = ['Pomadas e Finalizadores', 'Cuidados com a Barba', 'Shampoos e Condicionadores', 'Lâminas e Descartáveis', 'Bebidas']
categorias = {}
for cat_name in categorias_data:
    cat, _ = Category.objects.get_or_create(tenant=tenant, name=cat_name)
    categorias[cat_name] = cat

# Create Products
produtos_data = [
    {
        'name': 'Pomada Modeladora Efeito Matte', 'sku': 'POM-MATTE-01', 'category': categorias['Pomadas e Finalizadores'],
        'supplier': fornecedores['Beleza Profissional Distribuidora'], 'unit': 'un', 'cost_price': 15.00, 'sale_price': 45.00,
        'current_stock': 20, 'min_stock_alert': 5, 'tracks_batches': False
    },
    {
        'name': 'Óleo Hidratante para Barba Premium', 'sku': 'OLEO-BRB-01', 'category': categorias['Cuidados com a Barba'],
        'supplier': fornecedores['Barber Shop Suprimentos'], 'unit': 'un', 'cost_price': 22.00, 'sale_price': 60.00,
        'current_stock': 15, 'min_stock_alert': 3, 'tracks_batches': False
    },
    {
        'name': 'Balm para Barba', 'sku': 'BALM-BRB-01', 'category': categorias['Cuidados com a Barba'],
        'supplier': fornecedores['Barber Shop Suprimentos'], 'unit': 'un', 'cost_price': 18.00, 'sale_price': 55.00,
        'current_stock': 12, 'min_stock_alert': 4, 'tracks_batches': False
    },
    {
        'name': 'Shampoo Cabelo e Barba (Uso Lavatório)', 'sku': 'SHAMP-LAV-01', 'category': categorias['Shampoos e Condicionadores'],
        'supplier': fornecedores['Beleza Profissional Distribuidora'], 'unit': 'l', 'cost_price': 35.00, 'sale_price': 0.00,
        'current_stock': 5, 'min_stock_alert': 1, 'tracks_batches': False
    },
    {
        'name': 'Lâminas Gillette Wilkinson (Caixa 100un)', 'sku': 'LAM-WILK-01', 'category': categorias['Lâminas e Descartáveis'],
        'supplier': fornecedores['Barber Shop Suprimentos'], 'unit': 'cx', 'cost_price': 28.00, 'sale_price': 0.00,
        'current_stock': 8, 'min_stock_alert': 2, 'tracks_batches': False
    },
    {
        'name': 'Cerveja Heineken Long Neck', 'sku': 'CERV-HEIN-01', 'category': categorias['Bebidas'],
        'supplier': None, 'unit': 'un', 'cost_price': 5.00, 'sale_price': 12.00,
        'current_stock': 48, 'min_stock_alert': 12, 'tracks_batches': False
    }
]

for p_data in produtos_data:
    current_stock = p_data.pop('current_stock')
    product, created = Product.objects.get_or_create(tenant=tenant, name=p_data['name'], defaults=p_data)
    
    if created and current_stock > 0:
        # Create initial stock movement
        StockMovement.objects.create(
            tenant=tenant,
            product=product,
            type='in',
            quantity=current_stock,
            unit_price=product.cost_price,
            total_value=current_stock * product.cost_price,
            reason='initial',
            created_by=user
        )
        product.current_stock = current_stock
        product.save()

print("SUCCESS: Inventory seeded successfully!")
