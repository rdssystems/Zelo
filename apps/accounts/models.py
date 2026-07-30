import uuid

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("O e-mail é obrigatório.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", User.Role.SUPERADMIN)
        extra_fields.setdefault("tenant", None)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser precisa de is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser precisa de is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    """Usuário da plataforma. Login por e-mail; cliente final NÃO tem User
    (é identificado por telefone, ver apps.clients)."""

    class Role(models.TextChoices):
        SUPERADMIN = "superadmin", "Superadmin"
        TENANT_ADMIN = "tenant_admin", "Admin do salão"
        EMPLOYEE = "employee", "Funcionário"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = None
    email = models.EmailField("e-mail", unique=True)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="users",
    )
    role = models.CharField(
        "papel", max_length=20, choices=Role.choices, default=Role.EMPLOYEE
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        verbose_name = "usuário"
        verbose_name_plural = "usuários"
        constraints = [
            # tenant é null apenas para superadmin; obrigatório para os demais
            models.CheckConstraint(
                name="user_tenant_matches_role",
                condition=(
                    models.Q(role="superadmin", tenant__isnull=True)
                    | models.Q(role__in=["tenant_admin", "employee"], tenant__isnull=False)
                ),
            ),
        ]

    def __str__(self):
        return self.email

    @property
    def is_tenant_admin(self):
        return self.role == self.Role.TENANT_ADMIN

    @property
    def is_employee(self):
        return self.role == self.Role.EMPLOYEE
