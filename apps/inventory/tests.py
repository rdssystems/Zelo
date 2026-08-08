import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant

from . import services as inventory_ops
from .models import (
    Category,
    InventoryCountStatus,
    MovementReason,
    MovementType,
    PhysicalInventoryCount,
    PhysicalInventoryCountItem,
    Product,
    ProductBatch,
    StockMovement,
    StockMovementBatch,
    Supplier,
)

User = get_user_model()


def make_tenant_with_admin(slug):
    tenant = Tenant.objects.create(name=f"Salão {slug}", slug=slug)
    admin = User.objects.create_user(
        email=f"admin@{slug}.com",
        password="x",
        role=User.Role.TENANT_ADMIN,
        tenant=tenant,
    )
    return tenant, admin


def make_product(tenant, name="Shampoo", **overrides):
    defaults = {
        "unit": "un",
        "cost_price": Decimal("10.00"),
        "sale_price": Decimal("20.00"),
        "min_stock_alert": Decimal("5"),
    }
    defaults.update(overrides)
    return inventory_ops.create_product(tenant=tenant, name=name, **defaults)


class ProductDomainTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_create_product_starts_with_zero_stock(self):
        product = make_product(self.tenant, name="  Shampoo  ", sku=" SH-01 ")
        self.assertEqual(product.name, "Shampoo")
        self.assertEqual(product.sku, "SH-01")
        self.assertEqual(product.current_stock, Decimal("0"))
        self.assertTrue(product.is_active)

    def test_negative_cost_price_rejected(self):
        with self.assertRaises(ValidationError):
            make_product(self.tenant, cost_price=Decimal("-1"))

    def test_negative_min_stock_alert_rejected(self):
        with self.assertRaises(ValidationError):
            make_product(self.tenant, min_stock_alert=Decimal("-1"))

    def test_update_product(self):
        product = make_product(self.tenant)
        inventory_ops.update_product(
            product,
            name="Shampoo Reconstrutor",
            sku="SH-02",
            unit="ml",
            cost_price=Decimal("15.00"),
            sale_price=Decimal("30.00"),
            min_stock_alert=Decimal("10"),
        )
        product.refresh_from_db()
        self.assertEqual(product.name, "Shampoo Reconstrutor")
        self.assertEqual(product.unit, "ml")

    def test_toggle_active(self):
        product = make_product(self.tenant)
        inventory_ops.set_product_active(product, False)
        product.refresh_from_db()
        self.assertFalse(product.is_active)

    def test_delete_active_product_removes_it(self):
        """Excluir não exige mais desativar antes (decisão do usuário em
        2026-07-29) — o modal de confirmação do painel é a barreira contra
        clique acidental; só PROTECT (movimentação vinculada) bloqueia."""
        product = make_product(self.tenant)
        inventory_ops.delete_product(product)
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())

    def test_delete_inactive_product_without_movements(self):
        product = make_product(self.tenant)
        inventory_ops.set_product_active(product, False)
        inventory_ops.delete_product(product)
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())

    def test_delete_product_with_movements_rejected(self):
        product = make_product(self.tenant)
        inventory_ops.register_stock_movement(
            tenant=self.tenant,
            product=product,
            movement_type=MovementType.IN,
            quantity=Decimal("10"),
            unit_price=Decimal("10.00"),
            reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        with self.assertRaises(ValidationError):
            inventory_ops.delete_product(product)
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())

    def test_is_low_stock_property(self):
        product = make_product(self.tenant, min_stock_alert=Decimal("5"))
        self.assertTrue(product.is_low_stock)  # estoque 0 <= mínimo 5
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("10"),
            reason=MovementReason.PURCHASE, created_by=self.admin,
        )
        self.assertFalse(product.is_low_stock)


class RegisterStockMovementTest(TestCase):
    """CLAUDE.md regra 2: current_stock nunca muda fora daqui."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.product = make_product(cls.tenant)

    def test_in_movement_increases_stock(self):
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=self.product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("10.00"),
            reason=MovementReason.PURCHASE, created_by=self.admin,
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("10"))

    def test_out_movement_decreases_stock(self):
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=self.product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("10"),
            reason=MovementReason.PURCHASE, created_by=self.admin,
        )
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=self.product, movement_type=MovementType.OUT,
            quantity=Decimal("3"), unit_price=Decimal("20"),
            reason=MovementReason.LOSS, created_by=self.admin,
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("7"))

    def test_total_value_calculated(self):
        movement = inventory_ops.register_stock_movement(
            tenant=self.tenant, product=self.product, movement_type=MovementType.IN,
            quantity=Decimal("4"), unit_price=Decimal("2.50"),
            reason=MovementReason.PURCHASE, created_by=self.admin,
        )
        self.assertEqual(movement.total_value, Decimal("10.00"))

    def test_insufficient_stock_rejected(self):
        with self.assertRaises(ValidationError):
            inventory_ops.register_stock_movement(
                tenant=self.tenant, product=self.product, movement_type=MovementType.OUT,
                quantity=Decimal("1"), unit_price=Decimal("10"),
                reason=MovementReason.LOSS, created_by=self.admin,
            )
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal("0"))

    def test_zero_quantity_rejected(self):
        with self.assertRaises(ValidationError):
            inventory_ops.register_stock_movement(
                tenant=self.tenant, product=self.product, movement_type=MovementType.IN,
                quantity=Decimal("0"), unit_price=Decimal("10"),
                reason=MovementReason.PURCHASE, created_by=self.admin,
            )

    def test_negative_unit_price_rejected(self):
        with self.assertRaises(ValidationError):
            inventory_ops.register_stock_movement(
                tenant=self.tenant, product=self.product, movement_type=MovementType.IN,
                quantity=Decimal("1"), unit_price=Decimal("-1"),
                reason=MovementReason.PURCHASE, created_by=self.admin,
            )

    def test_cross_tenant_product_rejected(self):
        other_tenant, _ = make_tenant_with_admin("salao-b")
        other_product = make_product(other_tenant, name="Outro")
        with self.assertRaises(ValidationError):
            inventory_ops.register_stock_movement(
                tenant=self.tenant, product=other_product, movement_type=MovementType.IN,
                quantity=Decimal("1"), unit_price=Decimal("1"),
                reason=MovementReason.PURCHASE, created_by=self.admin,
            )

    def test_movement_rolled_back_if_stock_update_fails(self):
        """Garantia de atomicidade: se algo falhar, nem StockMovement nem
        current_stock ficam parcialmente aplicados."""
        count_before = StockMovement.objects.count()
        with self.assertRaises(ValidationError):
            inventory_ops.register_stock_movement(
                tenant=self.tenant, product=self.product, movement_type=MovementType.OUT,
                quantity=Decimal("999"), unit_price=Decimal("1"),
                reason=MovementReason.LOSS, created_by=self.admin,
            )
        self.assertEqual(StockMovement.objects.count(), count_before)


class ProductIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a, cls.admin_a = make_tenant_with_admin("salao-a")
        cls.tenant_b, cls.admin_b = make_tenant_with_admin("salao-b")
        cls.product_a = make_product(cls.tenant_a, name="Produto A")
        cls.product_b = make_product(cls.tenant_b, name="Produto B")

    def test_for_tenant_scopes_products(self):
        names = list(
            Product.objects.for_tenant(self.tenant_a).values_list("name", flat=True)
        )
        self.assertEqual(names, ["Produto A"])

    def test_api_list_only_own_tenant(self):
        client = APIClient()
        client.force_authenticate(self.admin_a)
        response = client.get("/api/v1/products/")
        names = [item["name"] for item in response.json()]
        self.assertEqual(names, ["Produto A"])

    def test_api_cannot_access_other_tenant_product(self):
        client = APIClient()
        client.force_authenticate(self.admin_a)
        response = client.get(f"/api/v1/products/{self.product_b.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_panel_list_only_own_tenant(self):
        self.client.force_login(self.admin_a)
        response = self.client.get("/painel/estoque/")
        self.assertContains(response, "Produto A")
        self.assertNotContains(response, "Produto B")


class ProductAPIPermissionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=cls.tenant
        )
        cls.category = inventory_ops.create_category(tenant=cls.tenant, name="Cabelo")

    def test_anonymous_denied(self):
        response = APIClient().get("/api/v1/products/")
        self.assertEqual(response.status_code, 403)

    def test_employee_can_read_but_not_write(self):
        client = APIClient()
        client.force_authenticate(self.employee)
        self.assertEqual(client.get("/api/v1/products/").status_code, 200)
        response = client.post(
            "/api/v1/products/",
            {"name": "Novo", "unit": "un", "cost_price": "5", "sale_price": "10", "min_stock_alert": "1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_can_create(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.post(
            "/api/v1/products/",
            {"name": "Creme", "category": self.category.pk, "unit": "un", "cost_price": "5",
             "sale_price": "10", "min_stock_alert": "1"},
        )
        self.assertEqual(response.status_code, 201)
        product = Product.objects.get(pk=response.json()["id"])
        self.assertEqual(product.tenant, self.tenant)

    def test_current_stock_cannot_be_set_via_api(self):
        """Regra 2 do CLAUDE.md: current_stock é read-only na API."""
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.post(
            "/api/v1/products/",
            {
                "name": "Creme 2", "category": self.category.pk, "unit": "un", "cost_price": "5",
                "sale_price": "10", "min_stock_alert": "1", "current_stock": "999",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["current_stock"], "0.00")

    def test_category_from_other_tenant_rejected(self):
        other_tenant, _ = make_tenant_with_admin("salao-b")
        other_category = inventory_ops.create_category(tenant=other_tenant, name="Outra")
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.post(
            "/api/v1/products/",
            {"name": "Creme 3", "category": other_category.pk, "unit": "un", "cost_price": "5",
             "sale_price": "10", "min_stock_alert": "1"},
        )
        self.assertEqual(response.status_code, 400)

    def test_admin_can_delete_active_product_via_api(self):
        product = make_product(self.tenant, name="Ativo")
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.delete(f"/api/v1/products/{product.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())

    def test_admin_can_register_movement_via_api(self):
        product = make_product(self.tenant, name="Condicionador")
        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.post(
            "/api/v1/stock-movements/",
            {
                "product": product.pk, "type": "in", "quantity": "5",
                "unit_price": "8.00", "reason": "purchase",
            },
        )
        self.assertEqual(response.status_code, 201)
        product.refresh_from_db()
        self.assertEqual(product.current_stock, Decimal("5"))

    def test_employee_cannot_register_movement_via_api(self):
        product = make_product(self.tenant, name="Condicionador")
        client = APIClient()
        client.force_authenticate(self.employee)
        response = client.post(
            "/api/v1/stock-movements/",
            {
                "product": product.pk, "type": "in", "quantity": "5",
                "unit_price": "8.00", "reason": "purchase",
            },
        )
        self.assertEqual(response.status_code, 403)


class ProductPanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=cls.tenant
        )
        cls.category = inventory_ops.create_category(tenant=cls.tenant, name="Cabelo")

    def test_login_required(self):
        response = self.client.get("/painel/estoque/")
        self.assertEqual(response.status_code, 302)

    def test_employee_forbidden(self):
        self.client.force_login(self.employee)
        response = self.client.get("/painel/estoque/")
        self.assertEqual(response.status_code, 403)

    def test_admin_creates_product_via_htmx(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/estoque/novo/",
            {"name": "Óleo Capilar", "sku": "", "category": self.category.pk, "unit": "ml",
             "cost_price": "12.00", "sale_price": "25.00", "min_stock_alert": "3"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Product.objects.filter(tenant=self.tenant, name="Óleo Capilar").exists())

    def test_admin_creates_product_with_comma_decimal_prices(self):
        """Campos de preço no painel são <input type="text"> (aceitam vírgula)."""
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/estoque/novo/",
            {"name": "Esmalte", "sku": "", "category": self.category.pk, "unit": "un",
             "cost_price": "3,50", "sale_price": "10,00", "min_stock_alert": "5,00"},
        )
        self.assertEqual(response.status_code, 200)
        product = Product.objects.get(tenant=self.tenant, name="Esmalte")
        self.assertEqual(product.cost_price, Decimal("3.50"))
        self.assertEqual(product.sale_price, Decimal("10.00"))
        self.assertEqual(product.min_stock_alert, Decimal("5.00"))

    def test_unit_must_be_a_valid_choice(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/estoque/novo/",
            {"name": "Produto X", "sku": "", "category": self.category.pk, "unit": "sacola",
             "cost_price": "1", "sale_price": "2", "min_stock_alert": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("HX-Retarget"), "#modal-slot")
        self.assertFalse(Product.objects.filter(name="Produto X").exists())

    def test_invalid_form_reopens_modal(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/estoque/novo/",
            {"name": "", "sku": "", "category": self.category.pk, "unit": "un",
             "cost_price": "-1", "sale_price": "10", "min_stock_alert": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("HX-Retarget"), "#modal-slot")
        self.assertEqual(Product.objects.count(), 0)

    def test_category_required_to_create_product(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/estoque/novo/",
            {"name": "Sem Categoria", "sku": "", "category": "", "unit": "un",
             "cost_price": "1", "sale_price": "2", "min_stock_alert": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("HX-Retarget"), "#modal-slot")
        self.assertFalse(Product.objects.filter(name="Sem Categoria").exists())

    def test_register_movement_via_htmx_updates_stock(self):
        product = make_product(self.tenant, name="Máscara Capilar")
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/estoque/{product.pk}/movimentar/",
            {"type": "in", "reason": "purchase", "quantity": "20", "unit_price": "9.90"},
        )
        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.current_stock, Decimal("20"))

    def test_register_movement_with_comma_decimal_values(self):
        product = make_product(self.tenant, name="Máscara Capilar")
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/estoque/{product.pk}/movimentar/",
            {"type": "in", "reason": "purchase", "quantity": "2,5", "unit_price": "9,90"},
        )
        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.current_stock, Decimal("2.5"))
        movement = StockMovement.objects.get(product=product)
        self.assertEqual(movement.unit_price, Decimal("9.90"))

    def test_movement_with_insufficient_stock_reopens_modal_with_error(self):
        product = make_product(self.tenant, name="Máscara Capilar")
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/estoque/{product.pk}/movimentar/",
            {"type": "out", "reason": "loss", "quantity": "5", "unit_price": "9.90"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("HX-Retarget"), "#modal-slot")
        self.assertContains(response, "Estoque insuficiente", status_code=200)
        product.refresh_from_db()
        self.assertEqual(product.current_stock, Decimal("0"))

    def test_low_stock_alert_rendered(self):
        make_product(self.tenant, name="Estoque Crítico", min_stock_alert=Decimal("10"))
        self.client.force_login(self.admin)
        response = self.client.get("/painel/estoque/")
        self.assertContains(response, "Estoque Crítico")
        self.assertContains(response, "warning")

    def test_stock_value_card_rounds_to_two_decimals(self):
        """current_stock (2 casas) * cost_price (2 casas) soma pra 4 casas no
        banco — o card 'Valor em Estoque' precisa arredondar de volta."""
        product = make_product(
            self.tenant, name="Óleo", cost_price=Decimal("12.34"), sale_price=Decimal("20.00")
        )
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type="in",
            quantity=Decimal("3"), unit_price=Decimal("12.34"),
            reason="purchase", created_by=self.admin,
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/estoque/")
        self.assertContains(response, "R$ 37,02")
        self.assertNotContains(response, "37,0200")

    def test_toggle_confirm_renders_app_modal_not_native(self):
        product = make_product(self.tenant, name="Shampoo")
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/estoque/{product.pk}/toggle/confirmar/")
        self.assertContains(response, "Desativar produto")
        self.assertContains(response, "Shampoo")
        self.assertNotContains(response, "hx-confirm")

    def test_delete_button_rendered_regardless_of_status(self):
        """Excluir está sempre disponível (com modal de confirmação) — não é
        mais preciso desativar antes (decisão do usuário em 2026-07-29)."""
        active = make_product(self.tenant, name="Ativo")
        inactive = make_product(self.tenant, name="Inativo")
        inventory_ops.set_product_active(inactive, False)
        self.client.force_login(self.admin)
        response = self.client.get("/painel/estoque/")
        self.assertContains(response, f"estoque/{inactive.pk}/excluir/confirmar/")
        self.assertContains(response, f"estoque/{active.pk}/excluir/confirmar/")

    def test_delete_inactive_product_via_panel(self):
        product = make_product(self.tenant, name="Descartável")
        inventory_ops.set_product_active(product, False)
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/estoque/{product.pk}/excluir/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())

    def test_delete_active_product_via_panel_succeeds(self):
        product = make_product(self.tenant, name="Pode")
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/estoque/{product.pk}/excluir/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())

    def test_delete_product_with_movements_via_panel_rejected(self):
        product = make_product(self.tenant, name="Não Pode")
        inventory_ops.register_stock_movement(
            tenant=self.tenant,
            product=product,
            movement_type=MovementType.IN,
            quantity=Decimal("10"),
            unit_price=Decimal("10.00"),
            reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/estoque/{product.pk}/excluir/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Não foi possível excluir")
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())

    def test_category_column_and_filter_rendered(self):
        make_product(self.tenant, name="Xampu Categorizado", category=self.category)
        self.client.force_login(self.admin)
        response = self.client.get("/painel/estoque/")
        self.assertContains(response, "Xampu Categorizado")
        self.assertContains(response, "Cabelo")

    def test_category_filter_scopes_product_list(self):
        other_category = inventory_ops.create_category(tenant=self.tenant, name="Unhas")
        make_product(self.tenant, name="Xampu", category=self.category)
        make_product(self.tenant, name="Esmalte", category=other_category)
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/estoque/?category={self.category.pk}")
        self.assertContains(response, "Xampu")
        self.assertNotContains(response, "Esmalte")

    def test_situacao_low_stock_filter_scopes_product_list(self):
        low = make_product(self.tenant, name="Xampu Baixo", min_stock_alert=Decimal("10"))
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=low, movement_type=MovementType.IN,
            quantity=Decimal("2"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )  # current_stock=2 <= min_stock_alert=10
        ok = make_product(self.tenant, name="Xampu Normal", min_stock_alert=Decimal("2"))
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=ok, movement_type=MovementType.IN,
            quantity=Decimal("50"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )  # current_stock=50 > min_stock_alert=2
        self.client.force_login(self.admin)
        response = self.client.get("/painel/estoque/?situacao=low_stock")
        self.assertContains(response, "Xampu Baixo")
        self.assertNotContains(response, "Xampu Normal")

    def test_situacao_expiring_batches_filter_scopes_product_list(self):
        expiring = make_batch_product(self.tenant, name="Esmalte Vencendo")
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=expiring, movement_type=MovementType.IN,
            quantity=Decimal("5"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin, batch_number="L1",
            expiry_date=datetime.date.today() + datetime.timedelta(days=5),
        )
        fine = make_batch_product(self.tenant, name="Esmalte Longe do Vencimento")
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=fine, movement_type=MovementType.IN,
            quantity=Decimal("5"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin, batch_number="L2",
            expiry_date=datetime.date.today() + datetime.timedelta(days=200),
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/estoque/?situacao=expiring_batches")
        self.assertContains(response, "Esmalte Vencendo")
        self.assertNotContains(response, "Esmalte Longe do Vencimento")

    def test_situacao_filter_options_rendered(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/estoque/")
        self.assertContains(response, "Estoque baixo")
        self.assertContains(response, "Lote vencendo")

    def test_low_stock_card_links_to_filter(self):
        self.client.force_login(self.admin)
        response = self.client.get("/painel/estoque/")
        self.assertContains(response, "/painel/estoque/?situacao=low_stock")
        self.assertContains(response, "/painel/estoque/?situacao=expiring_batches")


class CategoryDomainTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_create_category(self):
        category = inventory_ops.create_category(tenant=self.tenant, name="  Cabelo  ")
        self.assertEqual(category.name, "Cabelo")

    def test_blank_name_rejected(self):
        with self.assertRaises(ValidationError):
            inventory_ops.create_category(tenant=self.tenant, name="   ")

    def test_update_category(self):
        category = inventory_ops.create_category(tenant=self.tenant, name="Cabelo")
        inventory_ops.update_category(category, name="Cabelo e Couro Cabeludo")
        category.refresh_from_db()
        self.assertEqual(category.name, "Cabelo e Couro Cabeludo")

    def test_delete_category_without_products(self):
        category = inventory_ops.create_category(tenant=self.tenant, name="Descartável")
        inventory_ops.delete_category(category)
        self.assertFalse(Category.objects.filter(pk=category.pk).exists())

    def test_delete_category_with_products_rejected(self):
        category = inventory_ops.create_category(tenant=self.tenant, name="Cabelo")
        make_product(self.tenant, name="Xampu", category=category)
        with self.assertRaises(ValidationError):
            inventory_ops.delete_category(category)
        self.assertTrue(Category.objects.filter(pk=category.pk).exists())

    def test_duplicate_name_rejected_within_same_tenant(self):
        inventory_ops.create_category(tenant=self.tenant, name="Cabelo")
        with self.assertRaises(IntegrityError), transaction.atomic():
            inventory_ops.create_category(tenant=self.tenant, name="Cabelo")


class CategoryIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a, cls.admin_a = make_tenant_with_admin("salao-a")
        cls.tenant_b, _ = make_tenant_with_admin("salao-b")
        cls.category_a = inventory_ops.create_category(tenant=cls.tenant_a, name="Categoria A")
        cls.category_b = inventory_ops.create_category(tenant=cls.tenant_b, name="Categoria B")

    def test_for_tenant_scopes_categories(self):
        names = list(
            Category.objects.for_tenant(self.tenant_a).values_list("name", flat=True)
        )
        self.assertEqual(names, ["Categoria A"])

    def test_same_name_allowed_across_different_tenants(self):
        inventory_ops.create_category(tenant=self.tenant_a, name="Repetido")
        inventory_ops.create_category(tenant=self.tenant_b, name="Repetido")
        self.assertEqual(Category.objects.filter(name="Repetido").count(), 2)

    def test_panel_list_only_own_tenant(self):
        self.client.force_login(self.admin_a)
        response = self.client.get("/painel/estoque/categorias/")
        self.assertContains(response, "Categoria A")
        self.assertNotContains(response, "Categoria B")

    def test_panel_cannot_edit_other_tenant_category(self):
        self.client.force_login(self.admin_a)
        response = self.client.get(f"/painel/estoque/categorias/{self.category_b.pk}/editar/")
        self.assertEqual(response.status_code, 404)


class CategoryPanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=cls.tenant
        )

    def test_login_required(self):
        response = self.client.get("/painel/estoque/categorias/")
        self.assertEqual(response.status_code, 302)

    def test_employee_forbidden(self):
        self.client.force_login(self.employee)
        response = self.client.get("/painel/estoque/categorias/")
        self.assertEqual(response.status_code, 403)

    def test_admin_creates_category_via_htmx(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/estoque/categorias/nova/", {"name": "Cabelo"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Category.objects.filter(tenant=self.tenant, name="Cabelo").exists())

    def test_blank_name_reopens_modal(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/estoque/categorias/nova/", {"name": "   "}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("HX-Retarget"), "#modal-slot")
        self.assertEqual(Category.objects.count(), 0)

    def test_update_category_via_htmx(self):
        category = inventory_ops.create_category(tenant=self.tenant, name="Cabelo")
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/estoque/categorias/{category.pk}/editar/", {"name": "Cabelo e Couro"}
        )
        self.assertEqual(response.status_code, 200)
        category.refresh_from_db()
        self.assertEqual(category.name, "Cabelo e Couro")

    def test_delete_confirm_modal_has_no_native_confirm(self):
        category = inventory_ops.create_category(tenant=self.tenant, name="Cabelo")
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/estoque/categorias/{category.pk}/excluir/confirmar/")
        self.assertContains(response, "Excluir categoria")
        self.assertNotContains(response, "hx-confirm")

    def test_delete_category_without_products_via_panel(self):
        category = inventory_ops.create_category(tenant=self.tenant, name="Descartável")
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/estoque/categorias/{category.pk}/excluir/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Category.objects.filter(pk=category.pk).exists())

    def test_delete_category_with_products_via_panel_rejected(self):
        category = inventory_ops.create_category(tenant=self.tenant, name="Cabelo")
        make_product(self.tenant, name="Xampu", category=category)
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/estoque/categorias/{category.pk}/excluir/")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(Category.objects.filter(pk=category.pk).exists())

    def test_product_count_shown_in_list(self):
        category = inventory_ops.create_category(tenant=self.tenant, name="Cabelo")
        make_product(self.tenant, name="Xampu", category=category)
        make_product(self.tenant, name="Condicionador", category=category)
        self.client.force_login(self.admin)
        response = self.client.get("/painel/estoque/categorias/")
        self.assertContains(response, "Cabelo")
        self.assertContains(response, "2")


class SupplierDomainTest(TestCase):
    """RF43 — cadastro de fornecedor, preferido no produto ou vinculado a
    uma compra específica."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_create_supplier(self):
        supplier = inventory_ops.create_supplier(tenant=self.tenant, name="  Distribuidora XY  ")
        self.assertEqual(supplier.name, "Distribuidora XY")
        self.assertTrue(supplier.is_active)

    def test_blank_name_rejected(self):
        with self.assertRaises(ValidationError):
            inventory_ops.create_supplier(tenant=self.tenant, name="   ")

    def test_update_supplier(self):
        supplier = inventory_ops.create_supplier(tenant=self.tenant, name="Distribuidora XY")
        inventory_ops.update_supplier(
            supplier, name="Distribuidora XY Ltda", contact_name="Marcos",
            phone="11999998888", email="contato@xy.com", notes="Entrega às terças",
        )
        supplier.refresh_from_db()
        self.assertEqual(supplier.name, "Distribuidora XY Ltda")
        self.assertEqual(supplier.contact_name, "Marcos")

    def test_toggle_active(self):
        supplier = inventory_ops.create_supplier(tenant=self.tenant, name="Distribuidora XY")
        inventory_ops.set_supplier_active(supplier, False)
        supplier.refresh_from_db()
        self.assertFalse(supplier.is_active)

    def test_duplicate_name_rejected_within_same_tenant(self):
        inventory_ops.create_supplier(tenant=self.tenant, name="Distribuidora XY")
        with self.assertRaises(IntegrityError), transaction.atomic():
            inventory_ops.create_supplier(tenant=self.tenant, name="Distribuidora XY")

    def test_product_can_have_preferred_supplier(self):
        supplier = inventory_ops.create_supplier(tenant=self.tenant, name="Distribuidora XY")
        product = make_product(self.tenant, name="Xampu", supplier=supplier)
        self.assertEqual(product.supplier, supplier)

    def test_deleting_supplier_does_not_delete_product(self):
        """`Product.supplier` é SET_NULL — fornecedor nunca trava produto."""
        supplier = inventory_ops.create_supplier(tenant=self.tenant, name="Distribuidora XY")
        product = make_product(self.tenant, name="Xampu", supplier=supplier)
        supplier.delete()
        product.refresh_from_db()
        self.assertIsNone(product.supplier)
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())

    def test_stock_movement_can_reference_purchase_supplier(self):
        supplier = inventory_ops.create_supplier(tenant=self.tenant, name="Distribuidora XY")
        product = make_product(self.tenant, name="Xampu")
        movement = inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("10.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin, supplier=supplier,
        )
        self.assertEqual(movement.supplier, supplier)


class SupplierIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a, cls.admin_a = make_tenant_with_admin("salao-a")
        cls.tenant_b, _ = make_tenant_with_admin("salao-b")
        cls.supplier_a = inventory_ops.create_supplier(tenant=cls.tenant_a, name="Fornecedor A")
        cls.supplier_b = inventory_ops.create_supplier(tenant=cls.tenant_b, name="Fornecedor B")

    def test_for_tenant_scopes_suppliers(self):
        names = list(Supplier.objects.for_tenant(self.tenant_a).values_list("name", flat=True))
        self.assertEqual(names, ["Fornecedor A"])

    def test_same_name_allowed_across_different_tenants(self):
        inventory_ops.create_supplier(tenant=self.tenant_a, name="Repetido")
        inventory_ops.create_supplier(tenant=self.tenant_b, name="Repetido")
        self.assertEqual(Supplier.objects.filter(name="Repetido").count(), 2)

    def test_panel_list_only_own_tenant(self):
        self.client.force_login(self.admin_a)
        response = self.client.get("/painel/estoque/fornecedores/")
        self.assertContains(response, "Fornecedor A")
        self.assertNotContains(response, "Fornecedor B")

    def test_panel_cannot_edit_other_tenant_supplier(self):
        self.client.force_login(self.admin_a)
        response = self.client.get(f"/painel/estoque/fornecedores/{self.supplier_b.pk}/editar/")
        self.assertEqual(response.status_code, 404)


class SupplierPanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=cls.tenant
        )

    def test_login_required(self):
        response = self.client.get("/painel/estoque/fornecedores/")
        self.assertEqual(response.status_code, 302)

    def test_employee_forbidden(self):
        self.client.force_login(self.employee)
        response = self.client.get("/painel/estoque/fornecedores/")
        self.assertEqual(response.status_code, 403)

    def test_admin_creates_supplier_via_htmx(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/painel/estoque/fornecedores/novo/",
            {"name": "Distribuidora XY", "contact_name": "Marcos", "phone": "11999998888"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            Supplier.objects.filter(tenant=self.tenant, name="Distribuidora XY").exists()
        )

    def test_blank_name_reopens_modal(self):
        self.client.force_login(self.admin)
        response = self.client.post("/painel/estoque/fornecedores/novo/", {"name": "   "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("HX-Retarget"), "#modal-slot")
        self.assertEqual(Supplier.objects.count(), 0)

    def test_update_supplier_via_htmx(self):
        supplier = inventory_ops.create_supplier(tenant=self.tenant, name="Distribuidora XY")
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/estoque/fornecedores/{supplier.pk}/editar/", {"name": "Distribuidora XY Ltda"}
        )
        self.assertEqual(response.status_code, 200)
        supplier.refresh_from_db()
        self.assertEqual(supplier.name, "Distribuidora XY Ltda")

    def test_toggle_supplier_via_htmx(self):
        supplier = inventory_ops.create_supplier(tenant=self.tenant, name="Distribuidora XY")
        self.client.force_login(self.admin)
        response = self.client.post(f"/painel/estoque/fornecedores/{supplier.pk}/toggle/")
        self.assertEqual(response.status_code, 200)
        supplier.refresh_from_db()
        self.assertFalse(supplier.is_active)

    def test_product_count_shown_in_list(self):
        supplier = inventory_ops.create_supplier(tenant=self.tenant, name="Distribuidora XY")
        make_product(self.tenant, name="Xampu", supplier=supplier)
        make_product(self.tenant, name="Condicionador", supplier=supplier)
        self.client.force_login(self.admin)
        response = self.client.get("/painel/estoque/fornecedores/")
        self.assertContains(response, "Distribuidora XY")
        self.assertContains(response, "2")

    def test_product_form_offers_active_suppliers(self):
        supplier = inventory_ops.create_supplier(tenant=self.tenant, name="Distribuidora XY")
        self.client.force_login(self.admin)
        response = self.client.get("/painel/estoque/novo/")
        self.assertContains(response, "Distribuidora XY")

    def test_inactive_supplier_not_offered_in_product_form(self):
        supplier = inventory_ops.create_supplier(tenant=self.tenant, name="Distribuidora XY")
        inventory_ops.set_supplier_active(supplier, False)
        self.client.force_login(self.admin)
        response = self.client.get("/painel/estoque/novo/")
        self.assertNotContains(response, "Distribuidora XY")

    def test_create_product_with_supplier_via_panel(self):
        supplier = inventory_ops.create_supplier(tenant=self.tenant, name="Distribuidora XY")
        self.client.force_login(self.admin)
        category = inventory_ops.create_category(tenant=self.tenant, name="Cabelo")
        response = self.client.post(
            "/painel/estoque/novo/",
            {
                "name": "Xampu", "category": category.pk, "supplier": supplier.pk, "unit": "un",
                "cost_price": "10,00", "sale_price": "20,00", "min_stock_alert": "5",
            },
        )
        self.assertEqual(response.status_code, 200)
        product = Product.objects.get(tenant=self.tenant, name="Xampu")
        self.assertEqual(product.supplier, supplier)


def make_batch_product(tenant, name="Esmalte", **overrides):
    overrides.setdefault("tracks_batches", True)
    return make_product(tenant, name=name, **overrides)


class BatchDomainTest(TestCase):
    """RF44 — lote/validade com consumo FEFO."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_purchase_opens_batch_when_product_tracks_batches(self):
        product = make_batch_product(self.tenant)
        movement = inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin, batch_number="L1",
            expiry_date=datetime.date(2026, 12, 31),
        )
        batch = ProductBatch.objects.get(tenant=self.tenant, product=product)
        self.assertEqual(batch.batch_number, "L1")
        self.assertEqual(batch.quantity_received, Decimal("10"))
        self.assertEqual(batch.quantity_remaining, Decimal("10"))
        self.assertEqual(batch.unit_cost, Decimal("5.00"))
        product.refresh_from_db()
        self.assertEqual(product.current_stock, Decimal("10"))
        self.assertEqual(movement.quantity, Decimal("10"))

    def test_purchase_without_expiry_date_rejected(self):
        product = make_batch_product(self.tenant)
        with self.assertRaises(ValidationError):
            inventory_ops.register_stock_movement(
                tenant=self.tenant, product=product, movement_type=MovementType.IN,
                quantity=Decimal("10"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
                created_by=self.admin,
            )

    def test_adjustment_entry_does_not_open_batch(self):
        product = make_batch_product(self.tenant)
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("5.00"), reason=MovementReason.ADJUSTMENT,
            created_by=self.admin,
        )
        self.assertEqual(ProductBatch.objects.filter(tenant=self.tenant).count(), 0)

    def test_product_not_tracking_batches_never_opens_batch(self):
        product = make_product(self.tenant, tracks_batches=False)
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        self.assertEqual(ProductBatch.objects.filter(tenant=self.tenant).count(), 0)

    def test_out_consumes_batch_fefo_across_multiple_batches(self):
        product = make_batch_product(self.tenant)
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("5"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin, batch_number="EARLY",
            expiry_date=datetime.date(2026, 6, 30),
        )
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("5"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin, batch_number="LATE",
            expiry_date=datetime.date(2026, 12, 31),
        )
        out_movement = inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.OUT,
            quantity=Decimal("7"), unit_price=Decimal("15.00"), reason=MovementReason.SALE,
            created_by=self.admin,
        )
        early = ProductBatch.objects.get(tenant=self.tenant, batch_number="EARLY")
        late = ProductBatch.objects.get(tenant=self.tenant, batch_number="LATE")
        self.assertEqual(early.quantity_remaining, Decimal("0"))
        self.assertEqual(late.quantity_remaining, Decimal("3"))
        consumptions = StockMovementBatch.objects.filter(movement=out_movement).order_by(
            "batch__batch_number"
        )
        self.assertEqual(consumptions.count(), 2)
        self.assertEqual(consumptions.get(batch=early).quantity, Decimal("5"))
        self.assertEqual(consumptions.get(batch=late).quantity, Decimal("2"))

    def test_out_rejected_when_batches_have_insufficient_stock(self):
        product = make_batch_product(self.tenant)
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("5"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin, expiry_date=datetime.date(2026, 12, 31),
        )
        # `current_stock` teria saldo (5), mas os lotes cadastrados não —
        # simula um ajuste manual desalinhado; a saída deve travar no lote.
        product.current_stock = Decimal("50")
        product.save(update_fields=["current_stock"])
        with self.assertRaises(ValidationError):
            inventory_ops.register_stock_movement(
                tenant=self.tenant, product=product, movement_type=MovementType.OUT,
                quantity=Decimal("10"), unit_price=Decimal("15.00"), reason=MovementReason.SALE,
                created_by=self.admin,
            )

    def test_batches_expiring_soon(self):
        product = make_batch_product(self.tenant)
        soon = datetime.date.today() + datetime.timedelta(days=5)
        far = datetime.date.today() + datetime.timedelta(days=90)
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("1"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin, batch_number="SOON", expiry_date=soon,
        )
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("1"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin, batch_number="FAR", expiry_date=far,
        )
        expiring = list(inventory_ops.batches_expiring_soon(self.tenant, days=30))
        self.assertEqual([b.batch_number for b in expiring], ["SOON"])

    def test_depleted_batch_excluded_from_expiring_soon(self):
        product = make_batch_product(self.tenant)
        soon = datetime.date.today() + datetime.timedelta(days=5)
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("1"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin, batch_number="SOON", expiry_date=soon,
        )
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.OUT,
            quantity=Decimal("1"), unit_price=Decimal("15.00"), reason=MovementReason.SALE,
            created_by=self.admin,
        )
        self.assertEqual(inventory_ops.batches_expiring_soon(self.tenant, days=30).count(), 0)


class BatchIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a, cls.admin_a = make_tenant_with_admin("salao-a")
        cls.tenant_b, cls.admin_b = make_tenant_with_admin("salao-b")
        cls.product_a = make_batch_product(cls.tenant_a, name="Esmalte A")
        cls.product_b = make_batch_product(cls.tenant_b, name="Esmalte B")
        inventory_ops.register_stock_movement(
            tenant=cls.tenant_a, product=cls.product_a, movement_type=MovementType.IN,
            quantity=Decimal("5"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=cls.admin_a, batch_number="A1",
            expiry_date=datetime.date(2026, 12, 31),
        )
        inventory_ops.register_stock_movement(
            tenant=cls.tenant_b, product=cls.product_b, movement_type=MovementType.IN,
            quantity=Decimal("5"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=cls.admin_b, batch_number="B1",
            expiry_date=datetime.date(2026, 12, 31),
        )

    def test_for_tenant_scopes_batches(self):
        numbers = list(
            ProductBatch.objects.for_tenant(self.tenant_a).values_list("batch_number", flat=True)
        )
        self.assertEqual(numbers, ["A1"])

    def test_panel_cannot_view_other_tenant_product_batches(self):
        self.client.force_login(self.admin_a)
        response = self.client.get(f"/painel/estoque/{self.product_b.pk}/lotes/")
        self.assertEqual(response.status_code, 404)


class BatchPanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=cls.tenant
        )

    def test_login_required(self):
        product = make_batch_product(self.tenant)
        response = self.client.get(f"/painel/estoque/{product.pk}/lotes/")
        self.assertEqual(response.status_code, 302)

    def test_employee_forbidden(self):
        product = make_batch_product(self.tenant)
        self.client.force_login(self.employee)
        response = self.client.get(f"/painel/estoque/{product.pk}/lotes/")
        self.assertEqual(response.status_code, 403)

    def test_batches_list_shows_batch(self):
        product = make_batch_product(self.tenant)
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("5"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin, batch_number="L1",
            expiry_date=datetime.date(2026, 12, 31),
        )
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/estoque/{product.pk}/lotes/")
        self.assertContains(response, "L1")

    def test_movement_form_requires_expiry_date_for_batch_product(self):
        product = make_batch_product(self.tenant)
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/estoque/{product.pk}/movimentar/",
            {"type": "in", "reason": "purchase", "quantity": "5", "unit_price": "5,00"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ProductBatch.objects.filter(tenant=self.tenant).count(), 0)

    def test_missing_expiry_date_shows_inline_error_on_field(self):
        """A falta de validade tem que aparecer embaixo do campo "Validade"
        (form.expiry_date.errors), não só como aviso genérico no topo do
        modal — sem isso não dá pra saber qual campo faltou preencher."""
        product = make_batch_product(self.tenant)
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/estoque/{product.pk}/movimentar/",
            {"type": "in", "reason": "purchase", "quantity": "5", "unit_price": "5,00"},
        )
        self.assertIn("expiry_date", response.context["form"].errors)
        self.assertIn(
            "controla lote", " ".join(response.context["form"].errors["expiry_date"])
        )
        self.assertContains(response, 'id="id_expiry_date"')
        self.assertContains(response, "border-error")

    def test_movement_form_opens_batch_via_panel(self):
        product = make_batch_product(self.tenant)
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/estoque/{product.pk}/movimentar/",
            {
                "type": "in", "reason": "purchase", "quantity": "5", "unit_price": "5,00",
                "batch_number": "L1", "expiry_date": "2026-12-31",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            ProductBatch.objects.filter(tenant=self.tenant, batch_number="L1").exists()
        )

    def test_expiring_batches_count_shown_in_stock_list(self):
        product = make_batch_product(self.tenant)
        soon = datetime.date.today() + datetime.timedelta(days=5)
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("5"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin, batch_number="L1", expiry_date=soon,
        )
        self.client.force_login(self.admin)
        response = self.client.get("/painel/estoque/")
        self.assertContains(response, "Lotes Vencendo")

    def test_tracks_batches_toggle_via_product_form(self):
        self.client.force_login(self.admin)
        category = inventory_ops.create_category(tenant=self.tenant, name="Unhas")
        response = self.client.post(
            "/painel/estoque/novo/",
            {
                "name": "Esmalte", "category": category.pk, "unit": "un",
                "cost_price": "10,00", "sale_price": "20,00", "min_stock_alert": "5",
                "tracks_batches": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        product = Product.objects.get(tenant=self.tenant, name="Esmalte")
        self.assertTrue(product.tracks_batches)


class AverageCostDomainTest(TestCase):
    """RF45 — custo médio ponderado, recalculado a cada compra."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_first_purchase_sets_cost_to_purchase_price(self):
        product = make_product(self.tenant, cost_price=Decimal("10.00"))
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("8.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        product.refresh_from_db()
        self.assertEqual(product.cost_price, Decimal("8.00"))

    def test_second_purchase_weights_by_quantity(self):
        product = make_product(self.tenant, cost_price=Decimal("10.00"))
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("10.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("20.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        product.refresh_from_db()
        # (10*10 + 10*20) / 20 = 15.00
        self.assertEqual(product.cost_price, Decimal("15.00"))

    def test_adjustment_entry_does_not_change_cost(self):
        product = make_product(self.tenant, cost_price=Decimal("10.00"))
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("999.00"), reason=MovementReason.ADJUSTMENT,
            created_by=self.admin,
        )
        product.refresh_from_db()
        self.assertEqual(product.cost_price, Decimal("10.00"))

    def test_out_movement_does_not_change_cost(self):
        product = make_product(self.tenant, cost_price=Decimal("10.00"))
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("10.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.OUT,
            quantity=Decimal("3"), unit_price=Decimal("50.00"), reason=MovementReason.LOSS,
            created_by=self.admin,
        )
        product.refresh_from_db()
        self.assertEqual(product.cost_price, Decimal("10.00"))

    def test_rounds_to_two_decimal_places(self):
        product = make_product(self.tenant, cost_price=Decimal("10.00"))
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("3"), unit_price=Decimal("10.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("1"), unit_price=Decimal("1.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        product.refresh_from_db()
        # (3*10 + 1*1) / 4 = 7.75
        self.assertEqual(product.cost_price, Decimal("7.75"))

    def test_has_purchase_history_false_before_any_purchase(self):
        product = make_product(self.tenant)
        self.assertFalse(product.has_purchase_history)
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("5"), unit_price=Decimal("5.00"), reason=MovementReason.ADJUSTMENT,
            created_by=self.admin,
        )
        self.assertFalse(product.has_purchase_history)

    def test_has_purchase_history_true_after_purchase(self):
        product = make_product(self.tenant)
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("5"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        self.assertTrue(product.has_purchase_history)

    def test_update_product_ignores_manual_cost_price_after_purchase(self):
        product = make_product(self.tenant, cost_price=Decimal("10.00"))
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("10.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        inventory_ops.update_product(
            product, name=product.name, unit=product.unit, cost_price=Decimal("999.00"),
            sale_price=product.sale_price, min_stock_alert=product.min_stock_alert,
        )
        product.refresh_from_db()
        self.assertEqual(product.cost_price, Decimal("10.00"))

    def test_update_product_allows_manual_cost_price_before_any_purchase(self):
        product = make_product(self.tenant, cost_price=Decimal("10.00"))
        inventory_ops.update_product(
            product, name=product.name, unit=product.unit, cost_price=Decimal("12.50"),
            sale_price=product.sale_price, min_stock_alert=product.min_stock_alert,
        )
        product.refresh_from_db()
        self.assertEqual(product.cost_price, Decimal("12.50"))


class AverageCostPanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.category = inventory_ops.create_category(tenant=cls.tenant, name="Cabelo")

    def test_cost_price_field_disabled_after_purchase(self):
        product = make_product(self.tenant, name="Shampoo", cost_price=Decimal("10.00"))
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("10.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/estoque/{product.pk}/editar/")
        self.assertContains(response, 'id="id_cost_price"')
        self.assertContains(response, "disabled")

    def test_cost_price_field_enabled_before_any_purchase(self):
        product = make_product(self.tenant, name="Shampoo", cost_price=Decimal("10.00"))
        self.client.force_login(self.admin)
        response = self.client.get(f"/painel/estoque/{product.pk}/editar/")
        self.assertContains(response, 'id="id_cost_price"')
        self.assertNotContains(response, "disabled")

    def test_posting_cost_price_after_purchase_is_ignored(self):
        product = make_product(self.tenant, name="Shampoo", cost_price=Decimal("10.00"))
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("10.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/estoque/{product.pk}/editar/",
            {
                "name": "Shampoo", "category": self.category.pk, "unit": "un",
                "cost_price": "999,00", "sale_price": "20,00", "min_stock_alert": "5",
            },
        )
        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.cost_price, Decimal("10.00"))

    def test_purchase_via_movement_form_updates_cost_price(self):
        product = make_product(self.tenant, name="Shampoo", cost_price=Decimal("10.00"))
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/estoque/{product.pk}/movimentar/",
            {"type": "in", "reason": "purchase", "quantity": "10", "unit_price": "20,00"},
        )
        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.cost_price, Decimal("20.00"))


class PhysicalInventoryCountDomainTest(TestCase):
    """RF46 — inventário físico: contar, fechar, ajuste automático."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")

    def test_start_count_snapshots_active_products_only(self):
        active = make_product(self.tenant, name="Ativo")
        inactive = make_product(self.tenant, name="Inativo")
        inventory_ops.set_product_active(inactive, False)
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=active, movement_type=MovementType.IN,
            quantity=Decimal("7"), unit_price=Decimal("10"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        count = inventory_ops.start_inventory_count(tenant=self.tenant, created_by=self.admin)
        items = list(count.items.all())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].product, active)
        self.assertEqual(items[0].expected_quantity, Decimal("7"))
        self.assertIsNone(items[0].counted_quantity)

    def test_set_counted_quantity(self):
        product = make_product(self.tenant)
        count = inventory_ops.start_inventory_count(tenant=self.tenant, created_by=self.admin)
        item = count.items.get(product=product)
        inventory_ops.set_counted_quantity(item, Decimal("3"))
        item.refresh_from_db()
        self.assertEqual(item.counted_quantity, Decimal("3"))
        self.assertEqual(item.difference, Decimal("3"))

    def test_negative_counted_quantity_rejected(self):
        product = make_product(self.tenant)
        count = inventory_ops.start_inventory_count(tenant=self.tenant, created_by=self.admin)
        item = count.items.get(product=product)
        with self.assertRaises(ValidationError):
            inventory_ops.set_counted_quantity(item, Decimal("-1"))

    def test_close_generates_in_adjustment_for_surplus(self):
        product = make_product(self.tenant, cost_price=Decimal("10.00"))
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("5"), unit_price=Decimal("10"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        count = inventory_ops.start_inventory_count(tenant=self.tenant, created_by=self.admin)
        item = count.items.get(product=product)
        inventory_ops.set_counted_quantity(item, Decimal("8"))
        inventory_ops.close_inventory_count(count, created_by=self.admin)
        product.refresh_from_db()
        self.assertEqual(product.current_stock, Decimal("8"))
        adjustment = StockMovement.objects.get(
            tenant=self.tenant, product=product, reason=MovementReason.ADJUSTMENT
        )
        self.assertEqual(adjustment.type, MovementType.IN)
        self.assertEqual(adjustment.quantity, Decimal("3"))

    def test_close_generates_out_adjustment_for_shortage(self):
        product = make_product(self.tenant, cost_price=Decimal("10.00"))
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("10"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        count = inventory_ops.start_inventory_count(tenant=self.tenant, created_by=self.admin)
        item = count.items.get(product=product)
        inventory_ops.set_counted_quantity(item, Decimal("6"))
        inventory_ops.close_inventory_count(count, created_by=self.admin)
        product.refresh_from_db()
        self.assertEqual(product.current_stock, Decimal("6"))
        adjustment = StockMovement.objects.get(
            tenant=self.tenant, product=product, reason=MovementReason.ADJUSTMENT
        )
        self.assertEqual(adjustment.type, MovementType.OUT)
        self.assertEqual(adjustment.quantity, Decimal("4"))

    def test_close_ignores_blank_items(self):
        make_product(self.tenant, name="Não contado")
        count = inventory_ops.start_inventory_count(tenant=self.tenant, created_by=self.admin)
        inventory_ops.close_inventory_count(count, created_by=self.admin)
        self.assertEqual(
            StockMovement.objects.filter(tenant=self.tenant, reason=MovementReason.ADJUSTMENT).count(), 0
        )

    def test_close_skips_items_with_no_difference(self):
        product = make_product(self.tenant)
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("5"), unit_price=Decimal("10"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        count = inventory_ops.start_inventory_count(tenant=self.tenant, created_by=self.admin)
        item = count.items.get(product=product)
        inventory_ops.set_counted_quantity(item, Decimal("5"))
        inventory_ops.close_inventory_count(count, created_by=self.admin)
        self.assertEqual(
            StockMovement.objects.filter(tenant=self.tenant, reason=MovementReason.ADJUSTMENT).count(), 0
        )

    def test_close_marks_status_and_completed_at(self):
        make_product(self.tenant)
        count = inventory_ops.start_inventory_count(tenant=self.tenant, created_by=self.admin)
        inventory_ops.close_inventory_count(count, created_by=self.admin)
        count.refresh_from_db()
        self.assertEqual(count.status, InventoryCountStatus.COMPLETED)
        self.assertIsNotNone(count.completed_at)

    def test_cannot_set_counted_quantity_after_close(self):
        product = make_product(self.tenant)
        count = inventory_ops.start_inventory_count(tenant=self.tenant, created_by=self.admin)
        item = count.items.get(product=product)
        inventory_ops.close_inventory_count(count, created_by=self.admin)
        with self.assertRaises(ValidationError):
            inventory_ops.set_counted_quantity(item, Decimal("1"))

    def test_cannot_close_twice(self):
        make_product(self.tenant)
        count = inventory_ops.start_inventory_count(tenant=self.tenant, created_by=self.admin)
        inventory_ops.close_inventory_count(count, created_by=self.admin)
        with self.assertRaises(ValidationError):
            inventory_ops.close_inventory_count(count, created_by=self.admin)

    def test_close_consumes_fefo_batches_on_shortage(self):
        product = make_batch_product(self.tenant)
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("10"), unit_price=Decimal("5.00"), reason=MovementReason.PURCHASE,
            created_by=self.admin, batch_number="L1", expiry_date=datetime.date(2026, 12, 31),
        )
        count = inventory_ops.start_inventory_count(tenant=self.tenant, created_by=self.admin)
        item = count.items.get(product=product)
        inventory_ops.set_counted_quantity(item, Decimal("6"))
        inventory_ops.close_inventory_count(count, created_by=self.admin)
        batch = ProductBatch.objects.get(tenant=self.tenant, batch_number="L1")
        self.assertEqual(batch.quantity_remaining, Decimal("6"))


class PhysicalInventoryCountIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a, cls.admin_a = make_tenant_with_admin("salao-a")
        cls.tenant_b, cls.admin_b = make_tenant_with_admin("salao-b")
        cls.product_a = make_product(cls.tenant_a, name="Produto A")
        cls.product_b = make_product(cls.tenant_b, name="Produto B")
        cls.count_a = inventory_ops.start_inventory_count(tenant=cls.tenant_a, created_by=cls.admin_a)
        cls.count_b = inventory_ops.start_inventory_count(tenant=cls.tenant_b, created_by=cls.admin_b)

    def test_for_tenant_scopes_counts(self):
        counts = list(PhysicalInventoryCount.objects.for_tenant(self.tenant_a))
        self.assertEqual(counts, [self.count_a])

    def test_panel_cannot_view_other_tenant_count(self):
        self.client.force_login(self.admin_a)
        response = self.client.get(f"/painel/estoque/inventario/{self.count_b.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_panel_cannot_update_item_of_other_tenant_count(self):
        item_b = self.count_b.items.first()
        self.client.force_login(self.admin_a)
        response = self.client.post(
            f"/painel/estoque/inventario/{self.count_b.pk}/itens/{item_b.pk}/",
            {"counted_quantity": "5"},
        )
        self.assertEqual(response.status_code, 404)


class PhysicalInventoryCountPanelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant, cls.admin = make_tenant_with_admin("salao-a")
        cls.employee = User.objects.create_user(
            email="func@salao-a.com", password="x", role=User.Role.EMPLOYEE, tenant=cls.tenant
        )

    def test_login_required(self):
        response = self.client.get("/painel/estoque/inventario/")
        self.assertEqual(response.status_code, 302)

    def test_employee_forbidden(self):
        self.client.force_login(self.employee)
        response = self.client.get("/painel/estoque/inventario/")
        self.assertEqual(response.status_code, 403)

    def test_start_count_via_panel_redirects_to_detail(self):
        make_product(self.tenant)
        self.client.force_login(self.admin)
        response = self.client.post("/painel/estoque/inventario/nova/")
        self.assertEqual(response.status_code, 302)
        count = PhysicalInventoryCount.objects.get(tenant=self.tenant)
        self.assertEqual(response.url, f"/painel/estoque/inventario/{count.pk}/")

    def test_update_item_via_htmx(self):
        product = make_product(self.tenant, name="Shampoo")
        count = inventory_ops.start_inventory_count(tenant=self.tenant, created_by=self.admin)
        item = count.items.get(product=product)
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/painel/estoque/inventario/{count.pk}/itens/{item.pk}/",
            {"counted_quantity": "3,5"},
        )
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.counted_quantity, Decimal("3.5"))

    def test_close_via_panel_confirm_flow(self):
        product = make_product(self.tenant, cost_price=Decimal("10.00"))
        inventory_ops.register_stock_movement(
            tenant=self.tenant, product=product, movement_type=MovementType.IN,
            quantity=Decimal("5"), unit_price=Decimal("10"), reason=MovementReason.PURCHASE,
            created_by=self.admin,
        )
        count = inventory_ops.start_inventory_count(tenant=self.tenant, created_by=self.admin)
        item = count.items.get(product=product)
        self.client.force_login(self.admin)
        self.client.post(
            f"/painel/estoque/inventario/{count.pk}/itens/{item.pk}/",
            {"counted_quantity": "2"},
        )
        confirm_response = self.client.get(f"/painel/estoque/inventario/{count.pk}/fechar/confirmar/")
        self.assertEqual(confirm_response.status_code, 200)
        response = self.client.post(f"/painel/estoque/inventario/{count.pk}/fechar/")
        self.assertEqual(response.status_code, 200)
        count.refresh_from_db()
        self.assertEqual(count.status, InventoryCountStatus.COMPLETED)
        product.refresh_from_db()
        self.assertEqual(product.current_stock, Decimal("2"))

    def test_list_shows_counts(self):
        inventory_ops.start_inventory_count(tenant=self.tenant, created_by=self.admin)
        self.client.force_login(self.admin)
        response = self.client.get("/painel/estoque/inventario/")
        self.assertContains(response, "Em andamento")
